#!/usr/bin/env python
# -*- coding: utf-8 -*-

#          ***            **     ******************     ***         ***       ***           **               ***
#       ***              ****            **           ***           ****      ***          ****            ***
#     ***              ***  ***          **         ***             *****     ***        ***  ***        ***
#   ***               ***    ***         **       ***               *** ***   ***       ***    ***     ***
#  ***               ***      ***        **     ******************  ***  ***  ***      ***      ***   ******************
#    ***            ***        ***       **       ***               ***    ** ***     ***        ***    ***
#      ***         ***          ***      **         ***             ***     *****    ***          ***     ***
#         ***     ***            ***     **           ***           ***      ****   ***            ***      ***
#           ***  ***              ***    **             ***         ***       ***  ***              ***       ***

# Catenae
# Copyright (C) 2017-2019 Rodrigo Martínez Castaño
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import math
from threading import Lock, current_thread
from multiprocessing import Pipe
from pickle5 import pickle
import time
import argparse
from os import _exit, environ
from confluent_kafka import Producer, Consumer, KafkaError
import signal
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from socket import timeout
import json
from . import utils
from .electron import Electron
from .callback import Callback
from .logger import Logger
from .custom_queue import LinkQueue
from .custom_threading import Thread, ThreadPool
from .custom_multiprocessing import Process
from .connectors.aerospike import AerospikeConnector
from .connectors.mongodb import MongodbConnector
from .json_rpc import JsonRPC


_rpc_enabled_methods = set()
def rpc(method):
    """RPC decorator"""
    if method.__name__ not in _rpc_enabled_methods:
        _rpc_enabled_methods.add(method.__name__)
    return method

class Link:

    NO_INPUT_TOPIC_TIMEOUT = 1
    THREAD_LOOP_TIMEOUT = 3
    JSONRPC_MONITOR_TIMEOUT = 0.1
    JSONRPC_CALL_TIMEOUT = 5

    def __init__(self,
                 log_level='INFO',
                 input_mode='parity',
                 synchronous=False,
                 sequential=False,
                 uid_consumer_group=False,
                 num_rpc_threads=1,
                 num_main_threads=1,
                 input_topics=None,
                 output_topics=None,
                 kafka_endpoint='localhost:9092',
                 consumer_group=None,
                 consumer_timeout=300,
                 aerospike_endpoint=None,
                 mongodb_endpoint=None):

        self._set_log_level(log_level)
        self.logger = Logger(self, self._log_level)
        self.logger.log(f'log level: {self._log_level}')

        self._launched = False
        self._input_topics_lock = Lock()
        self._rpc_lock = Lock()

        # Preserve the id if the container restarts
        if 'CATENAE_DOCKER' in environ \
        and bool(environ['CATENAE_DOCKER']):
            self._uid = environ['HOSTNAME']
        else:
            self._uid = utils.get_uid()

        # RPC topics
        self._rpc_instance_topic = f'catenae_rpc_{self._uid}'
        self._rpc_group_topic = f'catenae_rpc_{self.__class__.__name__.lower()}'
        self._rpc_broadcast_topic = 'catenae_rpc_broadcast'
        self._rpc_topics = [self._rpc_instance_topic, self._rpc_group_topic, self._rpc_broadcast_topic]

        self._load_args()
        self._set_execution_opts(input_mode, synchronous, sequential, num_rpc_threads,
                                 num_main_threads, input_topics, output_topics, kafka_endpoint,
                                 consumer_timeout)
        self._set_connectors_properties(aerospike_endpoint, mongodb_endpoint)
        self._set_consumer_group(consumer_group, uid_consumer_group)

        self._input_messages = LinkQueue()
        self._output_messages = LinkQueue()
        self._jsonrpc_conn1, self._jsonrpc_conn2 = Pipe()
        self._changed_input_topics = False
        
        self._store = {'by_uid': dict(),
                       'by_group': dict()}
    
    def _set_connectors_properties(self, aerospike_endpoint, mongodb_endpoint):
        self._set_aerospike_properties(aerospike_endpoint)
        if hasattr(self, '_aerospike_host'):
            self.logger.log(f'aerospike_host: {self._aerospike_host}')
        if hasattr(self, '_aerospike_port'):
            self.logger.log(f'aerospike_port: {self._aerospike_port}')

        self._set_mongodb_properties(mongodb_endpoint)
        if hasattr(self, '_mongodb_host'):
            self.logger.log(f'mongodb_host: {self._mongodb_host}')
        if hasattr(self, '_mongodb_port'):
            self.logger.log(f'mongodb_port: {self._mongodb_port}')

    def _set_execution_opts(self, input_mode, synchronous, sequential, num_rpc_threads,
                            num_main_threads, input_topics, output_topics, kafka_endpoint,
                            consumer_timeout):

        if not hasattr(self, '_input_mode'):
            self._input_mode = input_mode
        self.logger.log(f'input_mode: {self._input_mode}')

        if hasattr(self, '_synchronous'):
            synchronous = self._synchronous

        if hasattr(self, '_sequential'):
            sequential = self._sequential

        if synchronous:
            self._synchronous = True
            self._sequential = True
        else:
            self._synchronous = False
            self._sequential = sequential

        if self._synchronous:
            self.logger.log('execution mode: sync + seq')
        else:
            if self._sequential:
                self.logger.log('execution mode: async + seq')
            else:
                self.logger.log('execution mode: async')

        if hasattr(self, '_num_rpc_threads'):
            num_rpc_threads = self._num_rpc_threads

        if hasattr(self, '_num_main_threads'):
            num_main_threads = self._num_main_threads

        if synchronous or sequential:
            self._num_main_threads = 1
            self._num_rpc_threads = 1
        else:
            self._num_rpc_threads = num_rpc_threads
            self._num_main_threads = num_main_threads
        self.logger.log(f'num_rpc_threads: {self._num_rpc_threads}')
        self.logger.log(f'num_main_threads: {self._num_main_threads}')

        if not hasattr(self, '_input_topics'):
            self._input_topics = input_topics
        self.logger.log(f'input_topics: {self._input_topics}')

        if not hasattr(self, '_output_topics'):
            self._output_topics = output_topics
        self.logger.log(f'output_topics: {self._output_topics}')

        if not hasattr(self, '_kafka_endpoint'):
            self._kafka_endpoint = kafka_endpoint
        self.logger.log(f'kafka_endpoint: {self._kafka_endpoint}')

        if not hasattr(self, '_consumer_timeout'):
            self._consumer_timeout = consumer_timeout
        self.logger.log(f'consumer_timeout: {self._consumer_timeout}')
        self._consumer_timeout = self._consumer_timeout * 1000

    @property
    def input_topics(self):
        return list(self._input_topics)

    @property
    def output_topics(self):
        return list(self._output_topics)

    @property
    def consumer_group(self):
        return self._consumer_group

    @property
    def args(self):
        return list(self._args)

    @property
    def uid(self):
        return self._uid

    @property
    def aerospike(self):
        return self._aerospike

    @property
    def mongodb(self):
        return self._mongodb

    def _loop_task(self, thread, target, args=None, kwargs=None, interval=None, wait=False):
        if wait:
            time.sleep(interval)

        while not thread.will_stop:
            try:
                self.logger.log(f'new loop iteration ({target.__name__})')
                start_timestamp = utils.get_timestamp()

                if args:
                    target(*args)
                elif kwargs:
                    target(**kwargs)
                else:
                    target()

                sleep_seconds = interval - utils.get_timestamp() + start_timestamp
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

            except Exception:
                self.logger.log(f'exception raised when executing the loop: {target.__name__}',
                                level='exception')

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGQUIT, self._signal_handler)

    def _signal_handler(self, sig, frame):
        if sig == signal.SIGINT:
            signal_name = 'SIGINT'
        elif sig == signal.SIGTERM:
            signal_mame = 'SIGINT'
        elif sig == signal.SIGQUIT:
            signal_mame = 'SIGQUIT'
        self.suicide(f'({signal_mame})')

    def _rpc_request_monitor(self):
        while not self._rpc_request_monitor_thread.will_stop:
            new_data = self._jsonrpc_conn1.poll()
            if not new_data:
                time.sleep(Link.JSONRPC_MONITOR_TIMEOUT)
                continue

            self._rpc_lock.acquire()
            is_notification, request = self._jsonrpc_conn1.recv()
            method, kwargs = request
            error_code = None
            result = None
            try:
                result = self._jsonrpc_call(method, kwargs)
            except JsonRPC.MethodNotFoundError:
                error_code = JsonRPC.METHOD_NOT_FOUND
            except JsonRPC.InternalError:
                error_code = JsonRPC.INTERNAL_ERROR
            finally:
                self._rpc_lock.release()

            if not is_notification:
                self._jsonrpc_conn1.send((error_code, result))


    def _rpc_enabled_method(self, method):
        if method in _rpc_enabled_methods:
            return True
        return False

    def _jsonrpc_call(self, method, kwargs=None):
        if not self._rpc_enabled_method(method):
            self.logger.log(f'method {method} cannot be called', level='debug')
            raise JsonRPC.MethodNotFoundError

        if kwargs is None:
            kwargs = {}

        try:
            return getattr(self, method)(**kwargs)
        except Exception:
            self.logger.log(level='exception')
            raise JsonRPC.InternalError

    # Throws socket.timeout
    def jsonrpc_call(self, uid, method, kwargs=None, request_id=None):
        instance_info = self._store['by_uid'][uid]
        
        url = f"{instance_info['scheme']}://{instance_info['host']}:{instance_info['port']}"
       
        request = {'jsonrpc': '2.0',
                   'method': method}
        if kwargs is not None:
            request.update({'params': kwargs})
        request.update({'id': request_id})
        data = bytes(json.dumps(request), 'utf-8')

        result = None
        try:
            response_data = urlopen(Request(url=url, data=data),
                                            timeout=Link.JSONRPC_CALL_TIMEOUT).read().decode('utf-8')
            result = json.loads(response_data)['result']
        except HTTPError as error:
            self.logger.log(f'HTTP error {error.code}', level='error')
            response_data = error.read().decode('utf-8')
            result = json.loads(response_data)['result']

        return result

    def _it_is_me(self, host, port):
        if host == environ['JSONRPC_HOST'] and \
           port == environ['JSONRPC_PORT']:
           return True
        return False

    def _is_instance_available(self, host, port, scheme):
        request = {'jsonrpc': '2.0',
                   'method': 'available',
                   'id': 0}
        data = bytes(json.dumps(request), 'utf-8')

        url = f'{scheme}://{host}:{port}'
        try:
            response_data = urlopen(Request(url=url, data=data),
                                            timeout=Link.JSONRPC_CALL_TIMEOUT).read().decode('utf-8')
        except Exception:
            return False
        
        result = json.loads(response_data)['result']
        return result

    @property
    def store(self):
        return dict(self._store)

    @rpc
    def get_store(self):
        return self.store

    @rpc
    def available(self):
        return True

    @rpc
    def add_to_store(self, context, host=None, port=None, scheme=None):
        if self._it_is_me(host, port):
            return

        if not self._is_instance_available(host, port, scheme):
            return

        instance_info =  dict(host=host,
                              port=port,
                              scheme=scheme,
                              uid=context['uid'],
                              group=context['group'])

        self._store['by_uid'][context['uid']] = instance_info

        if not context['group'] in self._store['by_group']:
            self._store['by_group'][context['group']] = dict()
        self._store['by_group'][context['group']][context['uid']] = instance_info        

    def rpc_call(self, to='broadcast', method=None, args=None, kwargs=None):
        """ 
        Send a Kafka message which will be interpreted as an RPC call by the receiver module.
        """
        if not method:
            raise ValueError
        topic = f'catenae_rpc_{to.lower()}'
        electron = Electron(value={
            'method': method,
            'context': {
                'group': self._consumer_group,
                'uid': self._uid
            },
            'args': args,
            'kwargs': kwargs
        },
                            topic=topic)
        self.send(electron, synchronous=True)
    
    def _kafka_rpc_call(self, electron, commit_kafka_message_callback):
        method = electron.value['method']

        if not self._rpc_enabled_method(method):
            self.logger.log(f'method {method} cannot be called', level='debug')
            return

        self._rpc_lock.acquire()

        if not 'method' in electron.value:
            self.logger.log(f'invalid RPC invocation: {electron.value}', level='error')
            self._rpc_lock.release()
            return

        try:
            context = electron.value['context']
            context.update({'topic': electron.previous_topic})

            self.logger.log(
                f"RPC invocation from {electron.value['context']['uid']} ({electron.value['context']['group']})",
                level='debug')

            if electron.value['kwargs']:
                kwargs = electron.value['kwargs']
                kwargs.update({'context': context})
                getattr(self, method)(**kwargs)

            else:
                args = electron.value['args']
                if not args:
                    args = [context]
                elif type(args) != list:
                    args = [context, args]
                else:
                    args = [context] + args
                getattr(self, method)(*args)

        except Exception:
            self.logger.log(f'error when invoking {method} remotely', level='exception')

        finally:
            if self._synchronous:
                commit_kafka_message_callback.execute()
            self._rpc_lock.release()

    def suicide(self, message=None, exception=False):
        if message is None:
            message = '[SUICIDE]'
        else:
            message = f'[SUICIDE] {message}'

        if exception:
            self.logger.log(message, level='exception')
        else:
            self.logger.log(message, level='warn')

        self._producer_thread.stop()
        self._input_handler_thread.stop()
        self._consumer_rpc_thread.stop()
        self._rpc_request_monitor_thread.stop()
        self._generator_main_thread.stop()
        self._consumer_main_thread.stop()

        for thread in self._transform_rpc_executor.threads:
            thread.stop()
        for thread in self._transform_main_executor.threads:
            thread.stop()

        self.logger.log('stopping threads...')

    def _join_if_not_current_thread(self, thread, name):
        if thread is not current_thread():
            thread.join()

    def loop(self, target, args=None, kwargs=None, interval=60, wait=False):
        loop_task_kwargs = {
            'target': target,
            'args': args,
            'kwargs': kwargs,
            'interval': interval,
            'wait': wait
        }
        loop_thread = Thread(target=self._loop_task, kwargs=loop_task_kwargs)
        loop_task_kwargs.update({'thread': loop_thread})
        loop_thread.start()
        return loop_thread

    def launch_thread(self, target, args=None, kwargs=None):
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        thread = Thread(target=target, args=args, kwargs=kwargs)
        thread.start()
        return thread

    def launch_process(self, target, args=None, kwargs=None):
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        process = Process(target=target, args=args, kwargs=kwargs)
        process.start()
        return process

    def _kafka_producer(self):
        while not self._producer_thread.will_stop:
            try:
                electron = self._output_messages.get(timeout=Link.THREAD_LOOP_TIMEOUT, block=False)
            except LinkQueue.EmptyError:
                continue

            self._produce(electron)

    def _produce(self, electron, synchronous=None):
        # All the queue items of the _output_messages must be individual
        # instances of Electron
        if type(electron) != Electron:
            raise ValueError

        # The key is enconded for its use as partition key
        partition_key = None
        if electron.key:
            if type(electron.key) == str:
                partition_key = electron.key.encode('utf-8')
            else:
                partition_key = pickle.dumps(electron.key, protocol=pickle.HIGHEST_PROTOCOL)
        # Same partition key for the current instance if sequential mode
        # is enabled so consumer can get messages in order
        elif self._sequential:
            partition_key = self._uid.encode('utf-8')

        # If the destiny topic is not specified, the first is used
        if not electron.topic:
            if not self._output_topics:
                self.suicide('Electron / default output topic unset')
            electron.topic = self._output_topics[0]

        # Electrons are serialized
        if electron.unpack_if_string and type(electron.value) == str:
            serialized_electron = electron.value
        else:
            serialized_electron = pickle.dumps(electron.get_sendable(),
                                               protocol=pickle.HIGHEST_PROTOCOL)

        if synchronous is None:
            synchronous = self._synchronous

        if synchronous:
            producer = self._sync_producer
        else:
            producer = self._async_producer

        try:
            # If partition_key = None, the partition.assignment.strategy
            # is used to distribute the messages
            producer.produce(topic=electron.topic, key=partition_key, value=serialized_electron)

            # Asynchronous
            if not synchronous:
                producer.poll(0)

            # Synchronous
            else:
                # Wait for all messages in the Producer queue to be delivered.
                producer.flush()

            self.logger.log('electron produced', level='debug')

        except Exception:
            self.suicide('Kafka producer error', exception=True)

        # Synchronous
        if synchronous:
            for callback in electron.callbacks:
                callback.execute()

    def _transform(self, electron, commit_kafka_message_callback, transform_callback):
        self._rpc_lock.acquire()
        try:
            transform_result = self.transform(electron)
            self.logger.log('electron transformed', level='debug')
        except Exception:
            self.suicide('exception during the execution of "transform"', exception=True)
        finally:
            self._rpc_lock.release()

        if type(transform_result) == tuple:
            electrons = transform_result[0]
            # Function to call if asynchronous mode is enabled after
            # a message is correctly commited to a Kafka broker
            if len(transform_result) > 1:
                transform_callback.target = transform_result[1]
                if len(transform_result) > 2:
                    if type(transform_result[2]) == list:
                        transform_callback.args = transform_result[2]
                    elif type(transform_result[2]) == dict:
                        transform_callback.kwargs = transform_result[2]
        else:
            electrons = transform_result

        # Even if no electrons are returned in the transform method,
        # continue so the input electron can be commited by the Kafka
        # consumer (synchronous mode, kafka_output).
        if electrons is None:
            electrons = []

        # Already a list
        if type(electrons) == list:
            real_electrons = []
            for electron in electrons:
                if type(electron) == Electron:
                    real_electrons.append(electron)
                else:
                    real_electrons.append(Electron(value=electron, unpack_if_string=True))
            electrons = real_electrons

        # If there is only one item, convert it to a list
        else:
            if type(electrons) == Electron:
                electrons = [electrons]
            else:
                electrons = [Electron(value=electrons)]

        # If the transform method returns anything and the output
        # is set to the Kafka producer, delegate the remaining
        # work to the Kafka producer

        if electrons:
            # The callback will be executed only for the last
            # electron if there are more than one
            electrons[-1].callbacks = []
            if commit_kafka_message_callback:
                electrons[-1].callbacks.append(commit_kafka_message_callback)
            if transform_callback:
                electrons[-1].callbacks.append(transform_callback)

            for electron in electrons:
                self._output_messages.put(electron)
            return

        # If the synchronous mode is enabled, the input message
        # will be commited if the transform method returns None
        # or if the output is not managed by the Kafka producer
        if self._synchronous and commit_kafka_message_callback:
            commit_kafka_message_callback.execute()

    def _input_handler(self):
        while not self._input_handler_thread.will_stop:
            self.logger.log('waiting for a new electron to transform...', level='debug')

            transform_callback = Callback()
            commit_kafka_message_callback = Callback(type_=Callback.COMMIT_KAFKA_MESSAGE)

            queue_item = self._input_messages.get()
            self.logger.log('electron received', level='debug')

            # An item from the _input_messages queue will not be of
            # type Electron in any case. In the nearest scenario, the
            # Kafka message value would be an Electron instance

            # Tuple
            if type(queue_item) == tuple:
                commit_kafka_message_callback.target = queue_item[1]
                if len(queue_item) > 2:
                    if type(queue_item[2]) == list:
                        commit_kafka_message_callback.args = queue_item[2]
                    elif type(queue_item[2]) == dict:
                        commit_kafka_message_callback.kwargs = queue_item[2]
                queue_item = queue_item[0]

            # Electron instance
            if type(queue_item.value()) == Electron:
                electron = queue_item.value()

            # String or custom object
            elif type(queue_item.value()) == bytes:
                try:
                    # String
                    electron = Electron(queue_item.key(), queue_item.value().decode('utf-8'))
                except Exception:
                    # Other object
                    try:
                        electron = pickle.loads(queue_item.value())
                        if type(electron) != Electron:
                            electron = Electron(queue_item.key(), electron)
                    except Exception:
                        electron = Electron(queue_item.key(), queue_item.value())
            else:
                self.logger.log('Not supported type for ' + \
                f'{str(queue_item.value())} ({type(queue_item.value())})', level='error')
                continue

            # Clean the previous topic
            electron.previous_topic = queue_item.topic()
            electron.topic = None

            # The destiny topic will be overwritten if desired in the
            # transform method (default, first output topic)
            if electron.previous_topic in self._rpc_topics:
                self._transform_rpc_executor.submit(self._kafka_rpc_call,
                                                    [electron, commit_kafka_message_callback])
            else:
                self._transform_main_executor.submit(
                    self._transform, [electron, commit_kafka_message_callback, transform_callback])

    def _break_consumer_loop(self, subscription):
        return len(subscription) > 1 and self._input_mode != 'parity'

    def _commit_kafka_message(self, consumer, message):
        commited = False
        attempts = 0
        self.logger.log(
            f'trying to commit the message with value {message.value()} (attempt {attempts})',
            level='debug')
        while not commited:
            if attempts > 1:
                self.logger.log(
                    f'trying to commit the message with value {message.value()} (attempt {attempts})',
                    level='warn')
            try:
                consumer.commit(**{'message': message, 'asynchronous': False})
            except Exception:
                self.logger.log(
                    f'exception when trying to commit the message with value {message.value()}',
                    level='exception')
                continue
            commited = True
            attempts += 1
        self.logger.log(f'Message with value {message.value()} commited', level='debug')

    def _kafka_consumer_rpc(self):
        properties = dict(self._kafka_consumer_synchronous_properties)
        consumer = Consumer(properties)
        self.logger.log(f'[RPC] consumer properties: {utils.dump_dict_pretty(properties)}',
                        level='debug')
        subscription = list(self._rpc_topics)
        consumer.subscribe(subscription)
        self.logger.log(f'[RPC] listening on: {subscription}')

        while not self._consumer_rpc_thread.will_stop:
            try:
                message = consumer.poll(5)

                if not message or (not message.key() and not message.value()):
                    if not self._break_consumer_loop(subscription):
                        continue
                    # New topic / restart if there are more topics or
                    # there aren't assigned partitions
                    break

                if message.error():
                    # End of partition is not an error
                    if message.error().code() != KafkaError._PARTITION_EOF:
                        self.logger.log(str(message.error()), level='error')
                    continue

                # Commit when the transformation is commited
                self._input_messages.put((message, self._commit_kafka_message, [consumer, message]))

            except Exception:
                try:
                    consumer.close()
                finally:
                    self.logger.log('stopped RPC input')
                    self.suicide('Kafka consumer error', exception=True)

    def _kafka_consumer_main(self):
        if self._synchronous:
            properties = dict(self._kafka_consumer_synchronous_properties)
        else:
            properties = dict(self._kafka_consumer_common_properties)

        consumer = Consumer(properties)
        self.logger.log(f'[MAIN] consumer properties: {utils.dump_dict_pretty(properties)}',
                        level='debug')
        prev_queued_messages = 0

        while not self._consumer_main_thread.will_stop:
            while not self._input_topics:
                self.logger.log('No input topics, waiting...', level='debug')
                time.sleep(Link.NO_INPUT_TOPIC_TIMEOUT)

            self._set_input_topic_assignments()
            current_input_topic_assignments = dict(self._input_topic_assignments)

            for topic in current_input_topic_assignments.keys():
                with self._input_topics_lock:
                    if self._changed_input_topics:
                        self._changed_input_topics = False
                        break

                # Buffer for the current topic
                message_buffer = []

                if self._input_mode == 'exp':
                    subscription = [topic]
                elif self._input_mode == 'parity':
                    subscription = list(self._input_topics)
                else:
                    self.suicide('Unknown priority mode')

                # Replaces the current subscription
                consumer.subscribe(subscription)
                self.logger.log(f'[MAIN] listening on: {subscription}')

                try:
                    start_time = utils.get_timestamp_ms()
                    assigned_time = current_input_topic_assignments[topic]
                    while (assigned_time == -1 or Link.in_time(start_time, assigned_time)) \
                    and not self._consumer_main_thread.will_stop:
                        # Subscribe to the topics again if input topics have changed
                        with self._input_topics_lock:
                            if self._changed_input_topics:
                                # _changed_input_topics is set to False in the
                                # outer loop so both loops are broken
                                break

                        message = consumer.poll(Link.THREAD_LOOP_TIMEOUT)

                        if not message or (not message.key() and not message.value()):
                            if not self._break_consumer_loop(subscription):
                                continue
                            # New topic / restart if there are more topics or
                            # there aren't assigned partitions
                            break

                        if message.error():
                            # End of partition is not an error
                            if message.error().code() != KafkaError._PARTITION_EOF:
                                self.logger.log(str(message.error()), level='error')
                            continue

                        else:
                            # Synchronous commit
                            if self._synchronous:
                                # Commit when the transformation is commited
                                self._input_messages.put(
                                    (message, self._commit_kafka_message, [consumer, message]))
                                continue

                            # Asynchronous (only one topic)
                            if len(subscription) == 1 or self._input_mode == 'parity':
                                self._input_messages.put(message)
                                continue

                            # Asynchronous (with penalizations support for
                            # multiple topics)

                            # The message is added to a local list that will be
                            # dumped to a queue for asynchronous processing
                            message_buffer.append(message)
                            current_queued_messages = len(message_buffer)

                            self._input_messages.decrement_messages_left()

                            # If there is only one message left, the offset is
                            # committed
                            if self._input_messages.messages_left < 1:
                                for message in message_buffer:
                                    self._input_messages.put(message)
                                message_buffer = []

                                self._input_messages.reset_messages_left()

                            # Penalize if only one message was consumed
                            if not self._break_consumer_loop(subscription) \
                            and current_queued_messages > 1 \
                            and current_queued_messages > prev_queued_messages - 2:
                                self.logger.log(f'penalized topic: {topic}')
                                break

                            prev_queued_messages = current_queued_messages

                    # Dump the buffer before changing the subscription
                    for message in message_buffer:
                        self._input_messages.put(message)

                except Exception:
                    try:
                        consumer.close()
                    finally:
                        self.logger.log('stopped main input')
                        self.suicide('Kafka consumer error', exception=True)

    def _get_index_assignment(self, window_size, index, elements_no, base=1.7):
        """
        window_size implies a full cycle consuming all the queues with
        priority.
        """
        aggregated_value = .0

        # The first element has the biggest value
        reverse_index = elements_no - index - 1

        for index in range(elements_no):
            value = math.pow(base, index)
            if index is reverse_index:
                index_assignment = value
            aggregated_value += value

        return (index_assignment / aggregated_value) * window_size

    def setup(self):
        pass

    def transform(self, _):
        for thread in self._transform_main_executor.threads:
            thread.stop()

    def send(self, output_content, topic=None, callback=None, callbacks=None, synchronous=None):
        if type(output_content) == Electron:
            if topic:
                output_content.topic = topic
            electron = output_content.deepcopy()
        elif type(output_content != list):
            electron = Electron(value=output_content, topic=topic, unpack_if_string=True)
        elif type(output_content) == list:
            for item in output_content:
                self.send(item, topic=topic)

        if callback is not None:
            callbacks = [callback]
        if callbacks is not None:
            electron.callbacks = callbacks

        if synchronous is None:
            synchronous = self._synchronous
        if synchronous:
            self._produce(electron, synchronous=True)
        else:
            self._output_messages.put(electron)

    def generator(self):
        self.logger.log('Generator method undefined. Disabled.')

    def _thread_target(self, target, args=None, kwargs=None):
        try:
            if args:
                target(*args)
            elif kwargs:
                target(**kwargs)
            else:
                target()
        except Exception:
            self.suicide(f'Exception during the execution of "{target.__name__}"', exception=True)

    def add_input_topic(self, input_topic):
        if input_topic not in self._input_topics:
            self._input_topics.append(input_topic)
            with self._input_topics_lock:
                if self._input_mode == 'exp':
                    self._set_input_topic_assignments()
                self._changed_input_topics = True
            self.logger.log(f'added input {input_topic}')

    def remove_input_topic(self, input_topic):
        if input_topic in self._input_topics:
            self._input_topics.remove(input_topic)
            with self._input_topics_lock:
                if self._input_mode == 'exp':
                    self._set_input_topic_assignments()
                self._changed_input_topics = True
            self.logger.log(f'removed input {input_topic}')

    def start(self):
        if self._launched:
            return
        self._launched = True

        self._set_kafka_common_properties()
        self._set_connectors()
        self._setup_kafka_producers()

        # Overwritable by a link
        try:
            self.setup()
        except Exception:
            self.suicide('Exception during the execution of "setup"', exception=True)

        if self._kafka_endpoint:
            self._launch_threads()
            self._report_existence()

        self._setup_signal_handlers()
        self.logger.log(f'link {self._uid} is running')

        if self._kafka_endpoint:
            self._producer_thread.join()
            self._input_handler_thread.join()
            self._consumer_rpc_thread.join()
            self._rpc_request_monitor_thread.join()
            self._generator_main_thread.join()
            self._consumer_main_thread.join()

            for i, thread in enumerate(self._transform_rpc_executor.threads):
                self._join_if_not_current_thread(thread, f'{i} _transform_rpc_executor')
                thread.join()
            for i, thread in enumerate(self._transform_main_executor.threads):
                self._join_if_not_current_thread(thread, f'{i} _transform_rpc_executor')

        self.logger.log(f'link {self.uid} stopped')
        _exit(0)

    def _setup_kafka_producers(self):
        sync_producer_properties = dict(self._kafka_producer_synchronous_properties)
        self._sync_producer = Producer(sync_producer_properties)
        self.logger.log(
            f'sync producer properties: {utils.dump_dict_pretty(sync_producer_properties)}',
            level='debug')

        async_producer_properties = dict(self._kafka_producer_common_properties)
        self._async_producer = Producer(async_producer_properties)
        self.logger.log(
            f'async producer properties: {utils.dump_dict_pretty(async_producer_properties)}',
            level='debug')

    def _launch_threads(self):
        # Kafka producer
        producer_kwargs = {'target': self._kafka_producer}
        self._producer_thread = Thread(target=self._thread_target, kwargs=producer_kwargs)
        self._producer_thread.start()

        # Transform
        self._transform_rpc_executor = ThreadPool(self, self._num_rpc_threads)
        self._transform_main_executor = ThreadPool(self, self._num_main_threads)
        transform_kwargs = {'target': self._input_handler}
        self._input_handler_thread = Thread(target=self._thread_target, kwargs=transform_kwargs)
        self._input_handler_thread.start()

        # Kafka RPC consumer
        consumer_kwargs = {'target': self._kafka_consumer_rpc}
        self._consumer_rpc_thread = Thread(target=self._thread_target, kwargs=consumer_kwargs)
        self._consumer_rpc_thread.start()

        # RPC requests thread
        self._rpc_request_monitor_thread = Thread(target=self._rpc_request_monitor)
        self._rpc_request_monitor_thread.start()

        # JSON-RPC
        Process(target=JsonRPC(self._jsonrpc_conn2, self.logger).run).start()

        # Generator
        self._generator_main_thread = Thread(target=self._thread_target,
                                             kwargs={'target': self.generator})
        self._generator_main_thread.start()

        # Kafka main consumer
        self._set_input_topic_assignments()
        self._consumer_main_thread = Thread(target=self._thread_target,
                                            kwargs={'target': self._kafka_consumer_main})
        self._consumer_main_thread.start()

    def _report_existence(self):
        kwargs = {'host': environ['JSONRPC_HOST'],
                  'port': environ['JSONRPC_PORT'],
                  'scheme': environ['JSONRPC_SCHEME']}
        self.rpc_call(to='broadcast', method='add_to_store', kwargs=kwargs)

    def _set_log_level(self, log_level):
        if not hasattr(self, '_log_level'):
            self._log_level = log_level.upper()

    def _set_connectors(self):
        try:
            self._aerospike = AerospikeConnector(self._aerospike_host,
                                                 self._aerospike_port,
                                                 connect=True)
        except AttributeError:
            self._aerospike = None

        try:
            self._mongodb = MongodbConnector(self._mongodb_host, self._mongodb_port, connect=True)
        except AttributeError:
            self._mongodb = None

    def _set_consumer_group(self, consumer_group, uid_consumer_group):
        if hasattr(self, 'consumer_group'):
            consumer_group = self._consumer_group
        if hasattr(self, 'uid_consumer_group'):
            uid_consumer_group = self._uid_consumer_group

        if uid_consumer_group:
            self._consumer_group = f'catenae_{self._uid}'
        elif consumer_group:
            self._consumer_group = consumer_group
        else:
            self._consumer_group = f'catenae_{self.__class__.__name__.lower()}'

        self.logger.log(f'consumer_group: {self._consumer_group}')

    def _set_kafka_common_properties(self):
        common_properties = {
            'bootstrap.servers': self._kafka_endpoint,
            'compression.codec': 'snappy',
            'api.version.request': True
        }

        self._kafka_consumer_common_properties = dict(common_properties)
        self._kafka_consumer_common_properties.update({
            'max.partition.fetch.bytes': 1048576,  # 1MiB,
            'metadata.max.age.ms': 10000,
            'socket.receive.buffer.bytes': 0,  # System default
            'group.id': self._consumer_group,
            'session.timeout.ms': 10000,  # heartbeat thread
            'max.poll.interval.ms': self._consumer_timeout,  # processing time
            'enable.auto.commit': True,
            'auto.commit.interval.ms': 5000,
            'default.topic.config': {
                'auto.offset.reset': 'smallest'
            }
        })

        self._kafka_consumer_synchronous_properties = dict(self._kafka_consumer_common_properties)
        self._kafka_consumer_synchronous_properties.update({
            'enable.auto.commit': False,
            'auto.commit.interval.ms': 0
        })

        self._kafka_producer_common_properties = dict(common_properties)
        self._kafka_producer_common_properties.update({
            'partition.assignment.strategy': 'roundrobin',
            'message.max.bytes': 1048576,  # 1MiB
            'socket.send.buffer.bytes': 0,  # System default
            'request.required.acks': 1,  # ACK from the leader
            # 'message.timeout.ms': 0, # (delivery.timeout.ms) Time a produced message waits for successful delivery
            # 'request.timeout.ms': 30000,
            'message.send.max.retries': 10,
            'queue.buffering.max.ms': 1,
            'max.in.flight.requests.per.connection': 1,
            'batch.num.messages': 1
        })

        self._kafka_producer_synchronous_properties = dict(self._kafka_producer_common_properties)
        self._kafka_producer_synchronous_properties.update({
            'message.send.max.retries': 10000000,  # Max value
            'request.required.acks': -1,  # ACKs from all replicas
            'max.in.flight.requests.per.connection': 1,
            'batch.num.messages': 1,
            # 'enable.idempotence': True, # not supported yet
        })

    def _set_input_topic_assignments(self):
        if self._input_mode == 'parity':
            self._input_topic_assignments = {-1: -1}

        elif self._input_mode == 'exp':
            self._input_topic_assignments = {}

            if len(self._input_topics[0]) == 1:
                self._input_topic_assignments[self._input_topics[0]] = -1

            window_size = 900  # in seconds, 15 minutes
            topics_no = len(self._input_topics)
            self.logger.log('input topics time assingments:')
            for index, topic in enumerate(self._input_topics):
                topic_assingment = \
                    self._get_index_assignment(window_size, index, topics_no)
                self._input_topic_assignments[topic] = topic_assingment
                self.logger.log(f' - {topic}: {topic_assingment} seconds')

    @staticmethod
    def in_time(start_time, assigned_time):
        return (start_time - utils.get_timestamp_ms()) < assigned_time

    @staticmethod
    def _parse_aerospike_args(parser):
        parser.add_argument('-a',
                            '--aerospike',
                            '--aerospike-bootstrap-server',
                            action="store",
                            dest="aerospike_endpoint",
                            help='Aerospike bootstrap server. \
                            E.g., "localhost:3000"',
                            required=False)

    def _set_aerospike_properties(self, aerospike_endpoint):
        if aerospike_endpoint is None:
            return
        host_port = aerospike_endpoint.split(':')
        if not hasattr(self, '_aerospike_host'):
            self._aerospike_host = host_port[0]
        if not hasattr(self, '_aerospike_port'):
            self._aerospike_port = int(host_port[1])

    @staticmethod
    def _parse_mongodb_args(parser):
        parser.add_argument('-m',
                            '--mongodb',
                            action="store",
                            dest="mongodb_endpoint",
                            help='MongoDB server. \
                            E.g., "localhost:27017"',
                            required=False)

    def _set_mongodb_properties(self, mongodb_endpoint):
        if mongodb_endpoint is None:
            return
        host_port = mongodb_endpoint.split(':')
        if not hasattr(self, '_mongodb_host'):
            self._mongodb_host = host_port[0]
        if not hasattr(self, '_mongodb_port'):
            self._mongodb_port = int(host_port[1])

    @staticmethod
    def _parse_kafka_args(parser):
        parser.add_argument('-i',
                            '--input',
                            action="store",
                            dest="input_topics",
                            help='Kafka input topics. Several topics ' +
                            'can be specified separated by commas',
                            required=False)
        parser.add_argument('-o',
                            '--output',
                            action="store",
                            dest="output_topics",
                            help='Kafka output topics. Several topics ' +
                            'can be specified separated by commas',
                            required=False)
        parser.add_argument('-k',
                            '--kafka-bootstrap-server',
                            action="store",
                            dest="kafka_endpoint",
                            help='Kafka bootstrap server. \
                            E.g., "localhost:9092"',
                            required=False)
        parser.add_argument('-g',
                            '--consumer-group',
                            action="store",
                            dest="consumer_group",
                            help='Kafka consumer group.',
                            required=False)
        parser.add_argument('--consumer-timeout',
                            action="store",
                            dest="consumer_timeout",
                            help='Kafka consumer timeout in seconds.',
                            required=False)

    def _set_kafka_properties_from_args(self, args):
        if args.input_topics:
            self._input_topics = args.input_topics.split(',')
        else:
            self._input_topics = []

        if args.output_topics:
            self._output_topics = args.output_topics.split(',')
        else:
            self._output_topics = []

        self._kafka_endpoint = args.kafka_endpoint

        if args.consumer_group:
            self._consumer_group = args.consumer_group

        if args.consumer_timeout:
            self._consumer_timeout = args.consumer_timeout

    @staticmethod
    def _parse_catenae_args(parser):
        parser.add_argument('--log-level',
                            action="store",
                            dest="log_level",
                            help='Catenae log level [debug|info|warning|error|critical].',
                            required=False)
        parser.add_argument('--input-mode',
                            action="store",
                            dest="input_mode",
                            help='Link input mode [parity|exp].',
                            required=False)
        parser.add_argument('--sync',
                            action="store_true",
                            dest="synchronous",
                            help='Synchronous mode is enabled.',
                            required=False)
        parser.add_argument('--seq',
                            action="store_true",
                            dest="sequential",
                            help='Sequential mode is enabled.',
                            required=False)
        parser.add_argument('--random-consumer-group',
                            action="store_true",
                            dest="uid_consumer_group",
                            help='Synchronous mode is disabled.',
                            required=False)
        parser.add_argument('--rpc-threads',
                            action="store",
                            dest="num_rpc_threads",
                            help='Number of RPC threads.',
                            required=False)
        parser.add_argument('--main-threads',
                            action="store",
                            dest="num_main_threads",
                            help='Number of main threads.',
                            required=False)

    def _set_catenae_properties_from_args(self, args):
        if args.log_level:
            self._log_level = args.log_level
        if args.input_mode:
            self._input_mode = args.input_mode
        if args.synchronous:
            self._synchronous = True
        if args.sequential:
            self._sequential = True
        if args.uid_consumer_group:
            self._uid_consumer_group = True
        if args.num_rpc_threads:
            self._num_rpc_threads = args.num_rpc_threads
        if args.num_main_threads:
            self._num_main_threads = args.num_main_threads

    def _load_args(self):
        parser = argparse.ArgumentParser()

        Link._parse_catenae_args(parser)
        Link._parse_kafka_args(parser)
        Link._parse_aerospike_args(parser)
        Link._parse_mongodb_args(parser)

        parsed_args = parser.parse_known_args()
        link_args = parsed_args[0]
        self._args = parsed_args[1]

        self._set_catenae_properties_from_args(link_args)
        self._set_kafka_properties_from_args(link_args)
        self._set_aerospike_properties(link_args.aerospike_endpoint)
        self._set_mongodb_properties(link_args.mongodb_endpoint)