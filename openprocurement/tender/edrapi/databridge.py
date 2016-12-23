from gevent import monkey
monkey.patch_all()

try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

import logging
import logging.config
import os
import argparse
from uuid import uuid4
import gevent
from openprocurement_client.client import TendersClientSync, TendersClient
from yaml import load
logger = logging.getLogger("openprocurement.tender.edrapi.databridge")


def generate_req_id():
    return b'edr-api-data-bridge-req-' + str(uuid4()).encode('ascii')


class EdrApiDataBridge(object):
    """ Edr API Data Bridge """

    def __init__(self, config):
        super(EdrApiDataBridge, self).__init__()
        self.config = config

        api_server = self.config_get('tenders_api_server')
        api_version = self.config_get('tenders_api_version')
        ro_api_server = self.config_get('public_tenders_api_server') or api_server

        self.tenders_sync_client = TendersClientSync('', host_url=ro_api_server, api_version=api_version)
        self.client = TendersClient(self.config_get('api_token'), host_url=api_server, api_version=api_version)

    def config_get(self, name):
        return self.config.get('main').get(name)

    def initialize_sync(self, params=None, direction=None):
        response = self.tenders_sync_client.sync_tenders(params, extra_headers={'X-Client-Request-ID': generate_req_id()})
        return response

    def get_tenders(self, params={}, direction=""):
        params['opt_fields'] = 'status'
        params['mode'] = '_all_'
        response = self.initialize_sync(params=params, direction=direction)
        tenders = response and response.data or []
        logger.info('Request returns list of tenders with lens {}'.format(len(tenders)))
        for tender in tenders:
            if tender['status'] == "active.qualification":
                yield tender
            else:
                logger.debug('Skipping tender {} in status {}'.format(tender['id'], tender['status']))

    def run(self):
        logger.error('Start EDR API Data Bridge')

        while True:
            gevent.sleep(10)
            try:
                for tender in self.get_tenders():
                    logger.info('Found tender {} with status {}.'.format(tender['id'], tender['status']))
            except KeyboardInterrupt:
                logger.info('Exiting...')
            except Exception, e:
                logger.error(e)


def main():
    parser = argparse.ArgumentParser(description='Edr API Data Bridge')
    parser.add_argument('config', type=str, help='Path to configuration file')
    parser.add_argument('--tender', type=str, help='Tender id to sync', dest="tender_id")
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        EdrApiDataBridge(config).run()
    else:
        logger.info('Invalid configuration file. Exiting...')


if __name__ == "__main__":
    main()
