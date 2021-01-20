# =================================================================
#
# Author: Etienne Pelletier <etienne.pelletier@canada.ca>
#         Felix Laframboise <felix.laframboise@canada.ca>
# Copyright (c) 2020 Etienne Pelletier
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

from datetime import datetime
import json
import logging
import os
from pathlib import Path

import click
from elasticsearch import logger as elastic_logger
from parse import parse

from msc_pygeoapi import cli_options
from msc_pygeoapi.connector.elasticsearch_ import ElasticsearchConnector
from msc_pygeoapi.env import (
    MSC_PYGEOAPI_LOGGING_LOGLEVEL,
)
from msc_pygeoapi.loader.base import BaseLoader
from msc_pygeoapi.util import (
    configure_es_connection,
    json_pretty_print,
    check_es_indexes_to_delete,
)

LOGGER = logging.getLogger(__name__)
elastic_logger.setLevel(getattr(logging, MSC_PYGEOAPI_LOGGING_LOGLEVEL))

# cleanup settings
DAYS_TO_KEEP = 366

# index settings
INDEX_BASENAME = 'aqhi_realtime_{}.'

MAPPINGS = {
    'forecasts': {
        'properties': {
            'geometry': {'type': 'geo_shape'},
            'properties': {
                'properties': {
                    'ID': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'aqhi_type': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'region_name_en': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'region_name_fr': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'region': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'datetime_utc': {
                        'type': 'date',
                        'format': 'strict_date_time_no_millis',
                    },
                    'datetime_text_en': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'datetime_text_fr': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'hourly_forecast_utc': {
                        'type': 'date',
                        'format': 'strict_date_time_no_millis',
                    },
                    'aqhi': {'type': 'byte'},
                }
            },
        }
    },
    'observations': {
        'properties': {
            'geometry': {'type': 'geo_shape'},
            'properties': {
                'properties': {
                    'ID': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'aqhi_type': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'region_name_en': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'region_name_fr': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'region': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'datetime_utc': {
                        'type': 'date',
                        'format': 'strict_date_time_no_millis',
                    },
                    'datetime_text_en': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'datetime_text_fr': {
                        'type': 'text',
                        'fields': {'raw': {'type': 'keyword'}},
                    },
                    'aqhi': {'type': 'float'},
                }
            },
        }
    },
}
SETTINGS = {
    'order': 0,
    'version': 1,
    'index_patterns': None,
    'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
    'mappings': None,
}


class AqhiRealtimeLoader(BaseLoader):
    """AQHI Real-time loader"""

    def __init__(self, conn_config={}):
        """initializer"""

        BaseLoader.__init__(self)

        self.conn = ElasticsearchConnector(conn_config)
        self.filepath = None
        self.type = None
        self.region = None
        self.date_ = None
        self.items = []

        for aqhi_type in MAPPINGS:
            template_name = INDEX_BASENAME.format(aqhi_type)
            SETTINGS['index_patterns'] = ['{}*'.format(template_name)]
            SETTINGS['mappings'] = MAPPINGS[aqhi_type]
            self.conn.create_template(template_name, SETTINGS)

    def parse_filename(self):
        """
        Parses a aqhi filename in order to get the date, forecast issued
        time, and region name.
        :return: `bool` of parse status
        """
        # parse filepath
        pattern = 'AQ_{type}_{region}_{date_}.json'
        filename = self.filepath.name
        parsed_filename = parse(pattern, filename)

        # set class attributes
        type_ = parsed_filename.named['type']
        if type_ == 'FCST':
            self.type = 'forecasts'
        if type_ == 'OBS':
            self.type = 'observations'

        self.region = parsed_filename.named['region']
        self.date_ = datetime.strptime(
            parsed_filename.named['date_'], '%Y%m%d%H%M'
        )

        return True

    def generate_geojson_features(self):
        """
        Generates and yields a series of aqhi forecasts or observations.
        Forecasts and observations are returned as Elasticsearch bulk API
        upsert actions,with documents in GeoJSON to match the Elasticsearch
        index mappings.
        :returns: Generator of Elasticsearch actions to upsert the AQHI
                  forecasts/observations
        """
        with open(self.filepath.resolve()) as f:
            data = json.load(f)
            if self.type == "forecasts":
                features = data['features']
            elif self.type == "observations":
                features = [data]

        for feature in features:
            # set document id
            feature['id'] = feature['ID']

            # clean out unnecessery properties
            feature.pop('ID')

            # set ES index name for feature
            es_index = '{}{}'.format(
                INDEX_BASENAME.format(self.type),
                self.date_.strftime('%Y-%m-%d'),
            )

            self.items.append(feature)

            action = {
                '_id': feature['id'],
                '_index': es_index,
                '_op_type': 'update',
                'doc': feature,
                'doc_as_upsert': True,
            }

            yield action

    def load_data(self, filepath):
        """
        loads data from event to target
        :returns: `bool` of status result
        """

        self.filepath = Path(filepath)

        # set class variables from filename
        self.parse_filename()

        LOGGER.debug('Received file {}'.format(self.filepath))

        # generate geojson features
        package = self.generate_geojson_features()
        self.conn.submit_elastic_package(package, request_size=80000)

        return True


@click.group()
def aqhi_realtime():
    """Manages AQHI indices"""
    pass


@click.command()
@click.pass_context
@cli_options.OPTION_FILE()
@cli_options.OPTION_DIRECTORY()
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
def add(ctx, file_, directory, es, username, password, ignore_certs):
    """Add aqhi data to Elasticsearch"""

    if all([file_ is None, directory is None]):
        raise click.ClickException('Missing --file/-f or --dir/-d option')

    conn_config = configure_es_connection(es, username, password, ignore_certs)

    files_to_process = []

    if file_ is not None:
        files_to_process = [file_]
    elif directory is not None:
        for root, dirs, files in os.walk(directory):
            for f in [file for file in files if file.endswith('.json')]:
                files_to_process.append(os.path.join(root, f))
        files_to_process.sort(key=os.path.getmtime)

    for file_to_process in files_to_process:
        loader = AqhiRealtimeLoader(conn_config)
        result = loader.load_data(file_to_process)
        if result:
            click.echo(
                'GeoJSON features generated: {}'.format(
                    json_pretty_print(loader.items)
                )
            )


@click.command()
@click.pass_context
@cli_options.OPTION_DAYS(
    default=DAYS_TO_KEEP,
    help='Delete indexes older than n days (default={})'.format(DAYS_TO_KEEP),
)
@cli_options.OPTION_DATASET(
    help='AQHI dataset indexes to delete.',
    type=click.Choice(['all', 'forecasts', 'observations']),
)
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
@cli_options.OPTION_YES(prompt='Are you sure you want to delete old indexes?')
def clean_indexes(ctx, days, dataset, es, username, password, ignore_certs):
    """Delete old AQHI realtime indexes"""

    conn_config = configure_es_connection(es, username, password, ignore_certs)
    conn = ElasticsearchConnector(conn_config)

    if dataset == 'all':
        indexes_to_fetch = '{}*'.format(INDEX_BASENAME.format('*'))
    else:
        indexes_to_fetch = '{}*'.format(INDEX_BASENAME.format(dataset))

    indexes = conn.get(indexes_to_fetch)

    if indexes:
        indexes_to_delete = check_es_indexes_to_delete(indexes, days)
        if indexes_to_delete:
            click.echo('Deleting indexes {}'.format(indexes_to_delete))
            conn.delete(','.join(indexes))

    click.echo('Done')


@click.command()
@click.pass_context
@cli_options.OPTION_DATASET(
    help='AQHI dataset indexes to delete.',
    type=click.Choice(['all', 'forecasts', 'observations']),
)
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
def delete_indexes(ctx, dataset, es, username, password, ignore_certs):
    """Delete all AQHI realtime indexes"""

    conn_config = configure_es_connection(es, username, password, ignore_certs)
    conn = ElasticsearchConnector(conn_config)

    if dataset == 'all':
        indexes = '{}*'.format(INDEX_BASENAME.format('*'))
    else:
        indexes = '{}*'.format(INDEX_BASENAME.format(dataset))

    click.echo('Deleting indexes {}'.format(indexes))

    conn.delete(indexes)

    click.echo('Done')


aqhi_realtime.add_command(add)
aqhi_realtime.add_command(clean_indexes)
aqhi_realtime.add_command(delete_indexes)
