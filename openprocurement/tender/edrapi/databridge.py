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
from gevent.queue import Queue

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
        buffers_size = self.config_get('buffers_size') or 500

        self.tenders_sync_client = TendersClientSync('', host_url=ro_api_server, api_version=api_version)
        self.client = TendersClient(self.config_get('api_token'), host_url=api_server, api_version=api_version)
        self.filtered_tenders_queue = Queue(maxsize=buffers_size)
        self.ids_queue = Queue(maxsize=buffers_size)
        self.initialization_event = gevent.event.Event()
        self.delay = self.config_get('delay') or 15

    def config_get(self, name):
        return self.config.get('main').get(name)

    def initialize_sync(self, params=None, direction=None):
        if direction == "backward":
            assert params['descending']
            response = self.tenders_sync_client.sync_tenders(params, extra_headers={'X-Client-Request-ID': generate_req_id()})
            # set values in reverse order due to 'descending' option
            self.initial_sync_point = {'forward_offset': response.prev_page.offset,
                                       'backward_offset': response.next_page.offset}
            self.initialization_event.set()  # wake up forward worker
            logger.info("Initial sync point {}".format(self.initial_sync_point))
            return response
        else:
            assert 'descending' not in params
            gevent.wait([self.initialization_event])
            params['offset'] = self.initial_sync_point['forward_offset']
            logger.info("Starting forward sync from offset {}".format(params['offset']))
            return self.tenders_sync_client.sync_tenders(params,
                                                         extra_headers={'X-Client-Request-ID': generate_req_id()})

    def get_tenders(self, params={}, direction=""):
        response = self.initialize_sync(params=params, direction=direction)

        while not (params.get('descending') and not len(response.data) and params.get('offset') == response.next_page.offset):
            tenders = response and response.data or []
            params['offset'] = response.next_page.offset
            logger.info('Response with list of tenders, lens {}'.format(len(tenders)))
            for tender in tenders:
                if (tender['status'] == "active.qualification" and
                    tender['procurementMethodType'] in ('aboveThresholdUA', 'aboveThresholdUA.defense', 'aboveThresholdEU',
                                                        'competitiveDialogueUA.stage2', 'competitiveDialogueEU.stage2'))\
                    or (tender['status'] == 'active.pre-qualification' and
                        tender['procurementMethodType'] in ('aboveThresholdEU', 'competitiveDialogueUA',
                                                            'competitiveDialogueEU')):
                    yield tender
                else:
                    logger.info('Skipping tender {} with status {} with procurementMethodType {}'.format(
                                tender['id'], tender['status'], tender['procurementMethodType']))
            logger.info('Sleep {} sync...'.format(direction))
            gevent.sleep(self.delay)
            response = self.tenders_sync_client.sync_tenders(params,
                                                             extra_headers={'X-Client-Request-ID': generate_req_id()})

    def get_tender_contracts_forward(self):
        logger.info('Start forward data sync worker...')
        params = {'opt_fields': 'status,procurementMethodType', 'mode': '_all_'}
        try:
            for tender in self.get_tenders(params=params, direction="forward"):
                logger.info('Forward sync: Put tender {} to process...'.format(tender['id']))
                self.filtered_tenders_queue.put(tender['id'])
        except Exception as e:
            logger.warn('Forward worker died!')
            logger.exception(e)
        else:
            logger.warn('Forward data sync finished!')

    def get_tender_contracts_backward(self):
        logger.info('Start backward data sync worker...')
        params = {'opt_fields': 'status,procurementMethodType', 'descending': 1, 'mode': '_all_'}
        try:
            for tender in self.get_tenders(params=params, direction="backward"):
                logger.info('Backward sync: Put tender {} to process...'.format(tender['id']))
                self.filtered_tenders_queue.put(tender['id'])
        except Exception as e:
            logger.warn('Backward worker died!')
            logger.exception(e)
        else:
            logger.info('Backward data sync finished.')

    def _start_synchronization_workers(self):
        logger.info('Starting forward and backward sync workers')
        self.jobs = [gevent.spawn(self.get_tender_contracts_backward),
                     gevent.spawn(self.get_tender_contracts_forward)]

    def _restart_synchronization_workers(self):
        logger.warn("Restarting synchronization")
        for j in self.jobs:
            j.kill()
        self._start_synchronization_workers()

    def run(self):
        logger.error('Start EDR API Data Bridge')
        self._start_synchronization_workers()
        backward_worker, forward_worker = self.jobs

        try:
            while True:
                gevent.sleep(self.delay)
                if forward_worker.dead or (backward_worker.dead and not backward_worker.successful()):
                    self._restart_synchronization_workers()
                    backward_worker, forward_worker = self.jobs
        except KeyboardInterrupt:
            logger.info('Exiting...')
        except Exception as e:
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
