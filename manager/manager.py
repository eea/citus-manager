
import typing
import retrying
import json
import logging

from kubernetes import client, config, watch
from kubernetes.client import V1Pod
from flask import Flask
from threading import Thread
from env_conf import parse_env_vars
from db import DBHandler
from config_monitor import ConfigMonitor, PodMonitorConfig


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler()],
)

log = logging.getLogger(__file__)


class ReadinessError(Exception):
    pass


class Manager:

    config_path = "/etc/citus-config/"

    def __init__(self) -> None:

        self.conf = parse_env_vars()
        self.db_handler = DBHandler(self.conf)
        self.init_provision = False

        self.citus_master_nodes: typing.Set[str] = set()
        self.citus_worker_nodes: typing.Set[str] = set()
        self.citus_coordinator_nodes: typing.Set[str] = set()
        self.start_web_server()
        self.pod_interactions: typing.Dict[
            str, typing.Dict[str, typing.Callable[[str], None]]
        ] = {
            "ADDED": {
                self.conf.master_label: self.add_master,
                self.conf.coordinator_label: self.add_coordinator,
                self.conf.worker_label: self.add_worker,
            },
            "DELETED": {
                self.conf.master_label: self.remove_master,
                self.conf.coordinator_label: self.remove_coordinator,
                self.conf.worker_label: self.remove_worker,
            },
        }
        self.config_monitor = self.create_provision_monitor()
        self.config_monitor.start_watchers()

    def create_provision_monitor(self) -> ConfigMonitor:
        master_config = PodMonitorConfig(
            self.citus_master_nodes,
            self.config_path + "master.setup",
            self.conf.master_service,
        )
        coordinator_config = PodMonitorConfig(
            self.citus_coordinator_nodes,
            self.config_path + "coordinator.setup",
            self.conf.coordinator_service,
        )
        worker_config = PodMonitorConfig(
            self.citus_worker_nodes,
            self.config_path + "worker.setup",
            self.conf.worker_service,
        )
        return ConfigMonitor(self.db_handler, master_config, coordinator_config, worker_config)

    @staticmethod
    def get_citus_type(pod: V1Pod) -> str:
        labels = pod.metadata.labels
        log.info("Retrieved labels: %s", labels)
        if not labels:
            return ""
        return labels.get("citusType", "")

    def run(self) -> None:
        log.info("Starting to watch citus db pods in {}".format(self.conf.namespace))

        config.load_incluster_config()  # or load_kube_config for external debugging
        api = client.CoreV1Api()
        w = watch.Watch()
        for event in w.stream(api.list_namespaced_pod, self.conf.namespace):
            citus_type, pod_name, event_type = self.parse_event(event)
            if not citus_type or event_type not in self.pod_interactions:
                continue
            handler = self.pod_interactions[event_type]
            if citus_type not in handler:
                log.error("Not recognized citus type %s", citus_type)
                continue
            try:
                handler[citus_type](pod_name)
            except ReadinessError as e:
                log.error(e)

    def parse_event(self, event: dict) -> typing.Tuple[str, str, str]:
        event_type = event["type"]
        pod = event["object"]
        citus_type = self.get_citus_type(pod)
        pod_name = pod.metadata.name
        if citus_type:
            log.info(
                "New event %s for pod %s with citus type %s",
                event_type,
                pod_name,
                citus_type,
            )
        return citus_type, pod_name, event_type

    def check_pod_readiness(self, pod_name: str) -> None:
        @retrying.retry(
            wait_fixed=5000,
            retry_on_exception=lambda x: not isinstance(x, client.rest.ApiException),
        )
        def request_pod_readiness():
            api = client.CoreV1Api()
            pod = api.read_namespaced_pod_status(pod_name, self.conf.namespace)
            status = pod.status
            readiness = [state.ready for state in status.container_statuses]
            log.info("Status: %s, %s", pod_name, readiness)
            assert all(readiness)
            log.info("Pod %s ready", pod_name)

        try:
            request_pod_readiness()
        except client.rest.ApiException as e:
            log.info("Error while waiting for pod readiness: %s", pod_name)
            raise ReadinessError(e)

    def start_web_server(self) -> None:
        app = Flask(__name__)

        @app.route("/registered")
        def registered_workers() -> str:
            pods = {
                "workers": list(self.citus_worker_nodes),
                "coordinator": list(self.citus_coordinator_nodes),
                "masters": list(self.citus_master_nodes),
            }
            return json.dumps(pods)

        Thread(target=app.run).start()

    def add_master(self, pod_name: str) -> None:
        self.check_pod_readiness(pod_name)
        log.info("Registering new master %s", pod_name)
        self.citus_master_nodes.add(pod_name)
        self.set_coordinator_master(pod_name)
        if len(self.citus_worker_nodes) >= self.conf.minimum_workers:
            self.config_monitor.provision_master(pod_name)
        for worker_pod in self.citus_worker_nodes:
            self.add_worker(worker_pod)

    def remove_master(self, pod_name: str) -> None:
        self.citus_master_nodes.discard(pod_name)
        log.info("Unregistered: %s", pod_name)

    def add_coordinator(self, pod_name: str) -> None:
        self.check_pod_readiness(pod_name)
        log.info("Registering new master %s", pod_name)
        self.citus_coordinator_nodes.add(pod_name)
        self.set_coordinator_coordinator(pod_name)
        if len(self.citus_worker_nodes) >= self.conf.minimum_workers:
            self.config_monitor.provision_coordinator(pod_name)
        for worker_pod in self.citus_worker_nodes:
            self.add_worker(worker_pod)

    def remove_coordinator(self, pod_name: str) -> None:
        self.citus_coordinator_nodes.discard(pod_name)
        log.info("Unregistered: %s", pod_name)

    def set_coordinator_master(self, pod_name: str) -> None:
        log.info("Registering new coordinator %s", pod_name)
        service_name = self.conf.master_service
        master_host = self.db_handler.get_host_name(
                pod_name, self.conf.master_service
            ) 
        query_params = {"host": master_host, "port": self.conf.pg_port}
        self.db_handler.execute_query(pod_name, service_name, """SELECT citus_set_coordinator_host(%(host)s)""", query_params)

    def set_coordinator_coordinator(self, pod_name: str) -> None:
        log.info("Registering new coordinator %s", pod_name)
        service_name = self.conf.coordinator_service
        coordinator_host = self.db_handler.get_host_name(
                pod_name, self.conf.coordinator_service
            ) 
        query_params = {"host": coordinator_host, "port": self.conf.pg_port}
        self.db_handler.execute_query(pod_name, service_name, """SELECT citus_set_coordinator_host(%(host)s)""", query_params)
        
    def add_worker(self, pod_name: str) -> None:
        self.check_pod_readiness(pod_name)
        log.info("Registering new worker %s", pod_name)
        self.citus_worker_nodes.add(pod_name)

        self.exec_on_masters("SELECT master_add_node(%(host)s, %(port)s)", pod_name)
        self.exec_on_coordinators("SELECT master_add_node(%(host)s, %(port)s)", pod_name)
        if len(self.citus_worker_nodes) >= self.conf.minimum_workers:
            if not self.init_provision:
                self.config_monitor.provision_all_nodes()
                self.init_provision = True
            else:
                self.config_monitor.provision_worker(pod_name)

    def remove_worker(self, worker_name: str) -> None:
        log.info("Worker terminated: %s", worker_name)
        self.citus_worker_nodes.discard(worker_name)
        self.exec_on_masters(
            """DELETE FROM pg_dist_shard_placement WHERE nodename=%(host)s AND nodeport=%(port)s;
            SELECT master_remove_node(%(host)s, %(port)s)""",
            worker_name,
        )
        log.info("Unregistered: %s", worker_name)

    def exec_on_masters(self, query: str, worker_name: str) -> None:
        for master in self.citus_master_nodes:
            worker_host = self.db_handler.get_host_name(
                worker_name, self.conf.worker_service
            )
            query_params = {"host": worker_host, "port": self.conf.pg_port}
            self.db_handler.execute_query(
                master, self.conf.master_service, query, query_params
            )

    def exec_on_coordinators(self, query: str, worker_name: str) -> None:
        for coordinator in self.citus_coordinator_nodes:
            worker_host = self.db_handler.get_host_name(
                worker_name, self.conf.worker_service
            )
            query_params = {"host": worker_host, "port": self.conf.pg_port}
            self.db_handler.execute_query(
                coordinator, self.conf.coordinator_service, query, query_params
            )

if __name__ == "__main__":
    manager = Manager()
    manager.run()