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
from openprocurement.tender.edrapi.client import EdrApiClient
from yaml import load
from gevent.queue import Queue
from collections import namedtuple
from openprocurement.tender.edrapi.journal_msg_ids import (
    DATABRIDGE_INFO, DATABRIDGE_SYNC_SLEEP, DATABRIDGE_GET_TENDER_FROM_QUEUE, DATABRIDGE_TENDER_PROCESS,
    DATABRIDGE_EMPTY_RESPONSE, DATABRIDGE_WORKER_DIED, DATABRIDGE_RESTART, DATABRIDGE_START)

logger = logging.getLogger("openprocurement.tender.edrapi.databridge")
Data = namedtuple('Data', ['tender_id', 'obj_id', 'code', 'obj_type'])


def generate_req_id():
    return b'edr-api-data-bridge-req-' + str(uuid4()).encode('ascii')


def journal_context(record={}, params={}):
    for k, v in params.items():
        record["JOURNAL_" + k] = v
    return record


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
        self.data_queue = Queue(maxsize=buffers_size)
        self.initialization_event = gevent.event.Event()
        self.until_too_many_requests_event = gevent.event.Event()
        self.until_too_many_requests_event.set()
        self.delay = self.config_get('delay') or 15
        self.edrApiClient = EdrApiClient(self.config_get('edr_api_server'), self.config_get('edr_api_token'))

    def config_get(self, name):
        return self.config.get('main').get(name)

    def initialize_sync(self, params=None, direction=None):
        self.initialization_event.clear()
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
                                tender['id'], tender['status'], tender['procurementMethodType']),
                                extra=journal_context({"MESSAGE_ID": DATABRIDGE_INFO},
                                params={"TENDER_ID": tender['id']}))
            logger.info('Sleep {} sync...'.format(direction), extra=journal_context({"MESSAGE_ID": DATABRIDGE_SYNC_SLEEP}))
            gevent.sleep(self.delay)
            response = self.tenders_sync_client.sync_tenders(params,
                                                             extra_headers={'X-Client-Request-ID': generate_req_id()})

    def prepare_data(self):
        while True:
            tender_id = self.filtered_tenders_queue.get()
            try:
                tender = self.tenders_sync_client.get_tender(
                                            tender_id, extra_headers={'X-Client-Request-ID': generate_req_id()})['data']
                logger.info('Get tender {} from filtered_tenders_queue'.format(tender_id),
                            extra=journal_context({"MESSAGE_ID": DATABRIDGE_GET_TENDER_FROM_QUEUE},
                            params={"TENDER_ID": tender['id']}))
            except Exception, e:
                logger.warn('Fail to get tender info {}'.format(tender_id),
                            extra=journal_context(params={"TENDER_ID": tender['id']}))
                logger.exception(e)
                logger.info('Put tender {} back to tenders queue'.format(tender_id),
                            extra=journal_context(params={"TENDER_ID": tender['id']}))
                self.filtered_tenders_queue.put(tender_id)
            else:
                if 'awards' in tender:
                    for award in tender['awards']:
                        logger.info('Processing tender {} award {}'.format(tender['id'], award['id']),
                                    extra=journal_context({"MESSAGE_ID": DATABRIDGE_TENDER_PROCESS},
                                    params={"TENDER_ID": tender['id']}))
                        if award['status'] == 'pending':
                            for supplier in award['suppliers']:
                                tender_data = Data(tender['id'], award['id'], supplier['identifier']['id'], 'award')
                                self.data_queue.put(tender_data)
                        else:
                            logger.info('Tender {} award {} is not in status pending.'.format(tender_id, award['id']),
                                        extra=journal_context(params={"TENDER_ID": tender['id']}))
                elif 'bids' in tender:
                    for bid in tender['bids']:
                        # get appropriate qualification id
                        logger.info('Processing tender {} bid {}'.format(tender['id'], bid['id']),
                                    extra=journal_context({"MESSAGE_ID": DATABRIDGE_TENDER_PROCESS},
                                    params={"TENDER_ID": tender['id']}))
                        qualification_id = [q['id'] for q in tender['qualifications'] if q['bidID'] == bid['id']][0]
                        for tenderer in bid['tenderers']:
                            tender_data = Data(tender['id'], qualification_id,
                                               tenderer['identifier']['id'], 'qualification')
                            self.data_queue.put(tender_data)

    def upload_document(self):
        while True:
            try:
                tender = self.data_queue.get()
                logger.info('Get tender {} from data_queue'.format(tender.tender_id),
                            extra=journal_context({"MESSAGE_ID": DATABRIDGE_GET_TENDER_FROM_QUEUE},
                                                  params={"TENDER_ID": tender.tender_id}))
            except Exception, e:
                logger.warn('Fail to get tender {} with {} id {} from edrpou queue'.format(
                            tender.tender_id, tender.obj_type, tender.obj_id),
                            extra=journal_context(params={"TENDER_ID": tender.tender_id}))
                logger.exception(e)
                logger.info('Put tender {} with {} id {} back to tenders queue'.format(
                            tender.tender_id, tender.obj_type, tender.obj_id),
                            extra=journal_context(params={"TENDER_ID": tender.tender_id}))
                self.filtered_tenders_queue.put((tender.tender_id, tender.obj_id, tender.code))
                gevent.sleep(self.delay)
            else:
                gevent.wait([self.until_too_many_requests_event])
                response = self.edrApiClient.get_by_code(tender.code)
                response_json = response.json()
                if response_json:
                    if response.status_code == 200:
                        # create patch request to award with document to upload
                        if tender.obj_type == 'award':
                            self.client.upload_award_document(response_json, tender.tender_id, tender.obj_id)
                        elif tender.obj_type == 'qualification':
                            self.client.upload_qualification_document(response_json, tender.tender_id, tender.obj_id)
                    elif response.status_code == 429:
                        self.until_too_many_requests_event.clear()
                        gevent.sleep(response.headers.get('Retry-After'))
                        self.until_too_many_requests_event.set()
                    elif response.status_code == 402:
                        logger.info('Payment required for requesting info to EDR. '
                                    'Error description: {err}'.format(err=response_json.get('errors')),
                                    extra=journal_context(params={"TENDER_ID": tender.tender_id}))
                    else:
                        logger.info('Error appeared while requesting to EDR. '
                                    'Description: {err}'.format(err=response_json.get('errors')),
                                    extra=journal_context(params={"TENDER_ID": tender.tender_id}))
                else:
                    logger.info('Response for {} and obj id {} is empty'.format(tender.tender_id, tender.obj_id),
                                extra=journal_context({"MESSAGE_ID": DATABRIDGE_EMPTY_RESPONSE},
                                                      params={"TENDER_ID": tender.tender_id}))

    def get_tenders_forward(self):
        logger.info('Start forward data sync worker...')
        params = {'opt_fields': 'status,procurementMethodType', 'mode': '_all_'}
        try:
            for tender in self.get_tenders(params=params, direction="forward"):
                logger.info('Forward sync: Put tender {} to process...'.format(tender['id']),
                            extra=journal_context({"MESSAGE_ID": DATABRIDGE_TENDER_PROCESS},
                                                  {"TENDER_ID": tender['id']}))
                self.filtered_tenders_queue.put(tender['id'])
        except Exception as e:
            logger.warn('Forward worker died!', extra=journal_context({"MESSAGE_ID": DATABRIDGE_WORKER_DIED}, {}))
            logger.exception(e)
        else:
            logger.warn('Forward data sync finished!')

    def get_tenders_backward(self):
        logger.info('Start backward data sync worker...')
        params = {'opt_fields': 'status,procurementMethodType', 'descending': 1, 'mode': '_all_'}
        try:
            for tender in self.get_tenders(params=params, direction="backward"):
                logger.info('Backward sync: Put tender {} to process...'.format(tender['id']),
                            extra=journal_context({"MESSAGE_ID": DATABRIDGE_TENDER_PROCESS},
                                                  {"TENDER_ID": tender['id']}))
                self.filtered_tenders_queue.put(tender['id'])
        except Exception as e:
            logger.warn('Backward worker died!', extra=journal_context({"MESSAGE_ID": DATABRIDGE_WORKER_DIED}, {}))
            logger.exception(e)
        else:
            logger.info('Backward data sync finished.')

    def _start_synchronization_workers(self):
        logger.info('Starting forward and backward sync workers')
        self.jobs = [gevent.spawn(self.get_tenders_backward),
                     gevent.spawn(self.get_tenders_forward)]

    def _restart_synchronization_workers(self):
        logger.warn('Restarting synchronization', extra=journal_context({"MESSAGE_ID": DATABRIDGE_RESTART}, {}))
        for j in self.jobs:
            j.kill()
        self._start_synchronization_workers()

    def _start_steps(self):
        self.immortal_jobs = {'prepare_data': gevent.spawn(self.prepare_data),
                              'upload_document': gevent.spawn(self.upload_document)}

    def run(self):
        logger.error('Start EDR API Data Bridge', extra=journal_context({"MESSAGE_ID": DATABRIDGE_START}, {}))
        self._start_synchronization_workers()
        self._start_steps()
        backward_worker, forward_worker = self.jobs

        try:
            while True:
                gevent.sleep(self.delay)
                if forward_worker.dead or (backward_worker.dead and not backward_worker.successful()):
                    self._restart_synchronization_workers()
                    backward_worker, forward_worker = self.jobs

                for name, job in self.immortal_jobs.items():
                    if job.dead:
                        logger.warn('Restarting {} worker'.format(name))
                        self.immortal_jobs[name] = gevent.spawn(getattr(self, name))
        except KeyboardInterrupt:
            logger.info('Exiting...')
            gevent.killall(self.jobs, timeout=5)
            gevent.killall(self.immortal_jobs, timeout=5)
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
