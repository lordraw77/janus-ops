"""Built-in templates for the well-known server software, one per product, from one table.

Each product gets what PostgreSQL and Apache have in `builtin_templates/*.json`, without a file
each: a `service` check (the systemd unit is active), a `port` check per listening port, an `http`
check where the product has a health URL, `detect` rules so the node's page suggests the template,
and macros whose values the agent finds (unit by glob, port by process). All of it needs only the
agent; nothing here carries a secret, a command or a query.

A product with a connector (PostgreSQL, Apache, Proxmox) keeps its own JSON file: those are not in
this table. Reading a product's own numbers (queries, status pages) is a connector in `probes/`.

Facts that are the weak point of a table like this, and how it handles them:
- `procs` are kernel command names, cut at 15 characters (`clickhouse-serv`). Java and Erlang
  products run as `java` / `beam.smp`: they have no `procs`, so only unit and port find them.
- `units` are the usual systemd unit names; distributions differ, so each is matched as a glob and
  the macro can be set by hand.
- Ports that every web server uses (80, 443, 8080 …) are checked but never used to *suggest* a
  template, or every node with a web server would be offered fifty of them.
"""

from dataclasses import dataclass
from typing import Any

# checked, but too common to prove a product is there
GENERIC_PORTS = frozenset({22, 25, 53, 80, 443, 3000, 5000, 8000, 8080, 8081, 8082, 8443, 9000})

Port = tuple[str, int, str]  # (macro, default, what it is)
Http = tuple[str, str, str, str]  # (port macro, path, accepted status, scheme)


@dataclass(frozen=True)
class Product:
    slug: str
    name: str
    category: str
    description: str
    units: tuple[str, ...] = ()
    procs: tuple[str, ...] = ()
    ports: tuple[Port, ...] = ()
    http: Http | None = None
    # take the first port from what its process listens on (only when the process listens on one
    # protocol of its own: not for a web server or a mail server with many listeners)
    auto_port: bool = True
    detect_ports: tuple[int, ...] | None = None


def _p(
    slug: str,
    name: str,
    category: str,
    description: str,
    units: tuple[str, ...] = (),
    procs: tuple[str, ...] = (),
    ports: tuple[Port, ...] = (),
    http: Http | None = None,
    auto_port: bool = True,
    detect_ports: tuple[int, ...] | None = None,
) -> Product:
    return Product(
        slug, name, category, description, units, procs, ports, http, auto_port, detect_ports
    )


def _main(port: int) -> tuple[Port, ...]:
    return (("PORT", port, "port"),)


# ------------------------------------------------------------------------------ databases
DATABASES = [
    _p(
        "mysql",
        "MySQL",
        "database",
        "MySQL server: service active and listening. MariaDB has its own template.",
        ("mysqld", "mysql"),
        ("mysqld",),
        _main(3306),
    ),
    _p(
        "mariadb",
        "MariaDB",
        "database",
        "MariaDB server: service active and listening.",
        ("mariadb", "mysqld"),
        ("mariadbd", "mariadb"),
        _main(3306),
    ),
    _p(
        "mongodb",
        "MongoDB",
        "database",
        "MongoDB server: service active and listening.",
        ("mongod",),
        ("mongod",),
        _main(27017),
    ),
    _p(
        "redis",
        "Redis",
        "database",
        "Redis server: service active and listening.",
        ("redis", "redis-server"),
        ("redis-server",),
        _main(6379),
    ),
    _p(
        "valkey",
        "Valkey",
        "database",
        "Valkey server (the Redis fork): service active and listening.",
        ("valkey", "valkey-server"),
        ("valkey-server",),
        _main(6379),
    ),
    _p(
        "memcached",
        "Memcached",
        "database",
        "Memcached: service active and listening.",
        ("memcached",),
        ("memcached",),
        _main(11211),
    ),
    _p(
        "elasticsearch",
        "Elasticsearch",
        "database",
        "Elasticsearch node: service, HTTP API port and an answer on it (401 counts).",
        ("elasticsearch",),
        (),
        (("PORT", 9200, "HTTP API"),),
        ("PORT", "/", "200-499", "http"),
    ),
    _p(
        "opensearch",
        "OpenSearch",
        "database",
        "OpenSearch node: service, HTTP API port and an answer on it (401 counts: security is on).",
        ("opensearch",),
        (),
        (("PORT", 9200, "HTTP API"),),
        ("PORT", "/", "200-499", "https"),
    ),
    _p(
        "mssql",
        "Microsoft SQL Server",
        "database",
        "SQL Server on Linux: service active and listening.",
        ("mssql-server",),
        ("sqlservr",),
        _main(1433),
    ),
    _p(
        "oracle-listener",
        "Oracle Database listener",
        "database",
        "Oracle TNS listener: process and port.",
        ("oracle*", "dbora"),
        ("tnslsnr",),
        _main(1521),
    ),
    _p(
        "clickhouse",
        "ClickHouse",
        "database",
        "ClickHouse server: service, native port and the /ping endpoint.",
        ("clickhouse-server",),
        ("clickhouse-serv",),
        (("PORT", 9000, "native"), ("HTTP_PORT", 8123, "HTTP")),
        ("HTTP_PORT", "/ping", "200", "http"),
        auto_port=False,
    ),
    _p(
        "cassandra",
        "Apache Cassandra",
        "database",
        "Cassandra node: service active and CQL port listening.",
        ("cassandra",),
        (),
        (("PORT", 9042, "CQL"),),
    ),
    _p(
        "scylladb",
        "ScyllaDB",
        "database",
        "ScyllaDB node: service active and CQL port listening.",
        ("scylla-server",),
        ("scylla",),
        (("PORT", 9042, "CQL"),),
    ),
    _p(
        "couchdb",
        "Apache CouchDB",
        "database",
        "CouchDB: service, port and an answer on it.",
        ("couchdb",),
        (),
        _main(5984),
        ("PORT", "/", "200", "http"),
    ),
    _p(
        "influxdb",
        "InfluxDB",
        "database",
        "InfluxDB: service, port and the /health endpoint (v2).",
        ("influxdb",),
        ("influxd",),
        _main(8086),
        ("PORT", "/health", "200", "http"),
    ),
    _p(
        "neo4j",
        "Neo4j",
        "database",
        "Neo4j: service, Bolt port and the HTTP browser port.",
        ("neo4j",),
        (),
        (("PORT", 7687, "Bolt"), ("HTTP_PORT", 7474, "HTTP")),
        ("HTTP_PORT", "/", "200", "http"),
    ),
    _p(
        "cockroachdb",
        "CockroachDB",
        "database",
        "CockroachDB node: service, SQL port and the /health endpoint.",
        ("cockroach*",),
        ("cockroach",),
        (("PORT", 26257, "SQL"), ("HTTP_PORT", 8080, "HTTP")),
        ("HTTP_PORT", "/health", "200", "http"),
        auto_port=False,
    ),
    _p(
        "etcd",
        "etcd",
        "database",
        "etcd member: service, client port and the /health endpoint.",
        ("etcd",),
        ("etcd",),
        _main(2379),
        ("PORT", "/health", "200", "http"),
    ),
    _p(
        "pgbouncer",
        "PgBouncer",
        "database",
        "PgBouncer connection pooler: service active and listening.",
        ("pgbouncer",),
        ("pgbouncer",),
        _main(6432),
    ),
    _p(
        "proxysql",
        "ProxySQL",
        "database",
        "ProxySQL: service, MySQL-facing port and the admin port.",
        ("proxysql",),
        ("proxysql",),
        (("PORT", 6033, "MySQL"), ("ADMIN_PORT", 6032, "admin")),
        auto_port=False,
    ),
]

# ------------------------------------------------------------------------------ web
WEB = [
    _p(
        "nginx",
        "nginx",
        "web",
        "nginx: service, HTTP and HTTPS ports and an answer over HTTP.",
        ("nginx",),
        ("nginx",),
        (("HTTP_PORT", 80, "HTTP"), ("HTTPS_PORT", 443, "HTTPS")),
        ("HTTP_PORT", "/", "200-499", "http"),
        auto_port=False,
    ),
    _p(
        "caddy",
        "Caddy",
        "web",
        "Caddy: service, HTTP and HTTPS ports.",
        ("caddy",),
        ("caddy",),
        (("HTTP_PORT", 80, "HTTP"), ("HTTPS_PORT", 443, "HTTPS")),
        auto_port=False,
    ),
    _p(
        "nginx-proxy-manager",
        "Nginx Proxy Manager",
        "web",
        "Nginx Proxy Manager: service, admin UI and API, proxy ports.",
        ("npm", "nginx-proxy-manager"),
        (),
        (
            ("ADMIN_PORT", 81, "admin UI"),
            ("HTTP_PORT", 80, "HTTP"),
            ("HTTPS_PORT", 443, "HTTPS"),
        ),
        ("ADMIN_PORT", "/api/", "200", "http"),
        auto_port=False,
        detect_ports=(81,),
    ),
    _p(
        "lighttpd",
        "lighttpd",
        "web",
        "lighttpd: service, port and an answer over HTTP.",
        ("lighttpd",),
        ("lighttpd",),
        (("HTTP_PORT", 80, "HTTP"),),
        ("HTTP_PORT", "/", "200-499", "http"),
        auto_port=False,
    ),
    _p(
        "openresty",
        "OpenResty",
        "web",
        "OpenResty: service, HTTP and HTTPS ports.",
        ("openresty",),
        ("openresty",),
        (("HTTP_PORT", 80, "HTTP"), ("HTTPS_PORT", 443, "HTTPS")),
        auto_port=False,
    ),
    _p(
        "haproxy",
        "HAProxy",
        "web",
        "HAProxy: service active and the front-end ports listening.",
        ("haproxy",),
        ("haproxy",),
        (("HTTP_PORT", 80, "HTTP"), ("HTTPS_PORT", 443, "HTTPS")),
        auto_port=False,
    ),
    _p(
        "traefik",
        "Traefik",
        "web",
        "Traefik: service, entry points and the dashboard/API port.",
        ("traefik",),
        ("traefik",),
        (("HTTP_PORT", 80, "HTTP"), ("HTTPS_PORT", 443, "HTTPS")),
        auto_port=False,
    ),
    _p(
        "envoy",
        "Envoy",
        "web",
        "Envoy proxy: service and the admin /ready endpoint.",
        ("envoy",),
        ("envoy",),
        (("ADMIN_PORT", 9901, "admin"),),
        ("ADMIN_PORT", "/ready", "200", "http"),
        auto_port=False,
    ),
    _p(
        "varnish",
        "Varnish Cache",
        "web",
        "Varnish: service, cache port and an answer over HTTP.",
        ("varnish",),
        ("varnishd",),
        (("PORT", 6081, "HTTP"),),
        ("PORT", "/", "200-599", "http"),
    ),
    _p(
        "squid",
        "Squid",
        "web",
        "Squid proxy: service active and listening.",
        ("squid",),
        ("squid",),
        _main(3128),
    ),
    _p(
        "kong",
        "Kong Gateway",
        "web",
        "Kong: service, proxy port and the Admin API /status endpoint.",
        ("kong",),
        (),
        (("PORT", 8000, "proxy"), ("ADMIN_PORT", 8001, "Admin API")),
        ("ADMIN_PORT", "/status", "200", "http"),
    ),
    _p(
        "tomcat",
        "Apache Tomcat",
        "web",
        "Tomcat: service active and HTTP connector listening.",
        ("tomcat*",),
        (),
        (("PORT", 8080, "HTTP"),),
        ("PORT", "/", "200-499", "http"),
    ),
    _p(
        "jetty",
        "Eclipse Jetty",
        "web",
        "Jetty: service active and HTTP connector listening.",
        ("jetty*",),
        (),
        (("PORT", 8080, "HTTP"),),
        ("PORT", "/", "200-499", "http"),
    ),
    _p(
        "wildfly",
        "WildFly",
        "web",
        "WildFly / JBoss: service, application port and management port.",
        ("wildfly*",),
        (),
        (("PORT", 8080, "HTTP"), ("ADMIN_PORT", 9990, "management")),
    ),
    _p(
        "glassfish",
        "GlassFish / Payara",
        "web",
        "GlassFish or Payara: service, application port and admin port.",
        ("glassfish*", "payara*"),
        (),
        (("PORT", 8080, "HTTP"), ("ADMIN_PORT", 4848, "admin")),
    ),
    _p(
        "gunicorn",
        "Gunicorn",
        "web",
        "Gunicorn (Python WSGI): service active and listening.",
        ("gunicorn*",),
        ("gunicorn",),
        _main(8000),
        auto_port=False,
    ),
    _p("uwsgi", "uWSGI", "web", "uWSGI: service active.", ("uwsgi*",), ("uwsgi",)),
    _p(
        "php-fpm",
        "PHP-FPM",
        "web",
        "PHP-FPM: service active (it often listens on a socket, so no port is checked).",
        ("php*-fpm", "php-fpm"),
        ("php-fpm",),
    ),
    _p(
        "trafficserver",
        "Apache Traffic Server",
        "web",
        "Traffic Server: service and proxy port.",
        ("trafficserver",),
        ("traffic_server",),
        _main(8080),
    ),
    _p(
        "nginx-unit",
        "NGINX Unit",
        "web",
        "NGINX Unit application server: service active.",
        ("unit",),
        ("unitd",),
    ),
]

# ------------------------------------------------------------------------------ messaging
MESSAGING = [
    _p(
        "rabbitmq",
        "RabbitMQ",
        "messaging",
        "RabbitMQ: service, AMQP port and the management UI.",
        ("rabbitmq-server",),
        (),
        (("PORT", 5672, "AMQP"), ("MGMT_PORT", 15672, "management")),
        ("MGMT_PORT", "/", "200", "http"),
    ),
    _p(
        "kafka",
        "Apache Kafka",
        "messaging",
        "Kafka broker: service and listener port.",
        ("kafka",),
        (),
        _main(9092),
    ),
    _p(
        "zookeeper",
        "Apache ZooKeeper",
        "messaging",
        "ZooKeeper: service and client port.",
        ("zookeeper",),
        (),
        _main(2181),
    ),
    _p(
        "activemq",
        "Apache ActiveMQ",
        "messaging",
        "ActiveMQ: service, OpenWire port and web console.",
        ("activemq",),
        (),
        (("PORT", 61616, "OpenWire"), ("WEB_PORT", 8161, "web console")),
    ),
    _p(
        "artemis",
        "ActiveMQ Artemis",
        "messaging",
        "Artemis broker: service, core port and web console.",
        ("artemis*",),
        (),
        (("PORT", 61616, "core"), ("WEB_PORT", 8161, "web console")),
    ),
    _p(
        "nats",
        "NATS",
        "messaging",
        "NATS server: service, client port and the monitoring /healthz endpoint.",
        ("nats-server", "nats"),
        ("nats-server",),
        (("PORT", 4222, "client"), ("MON_PORT", 8222, "monitoring")),
        ("MON_PORT", "/healthz", "200", "http"),
        auto_port=False,
    ),
    _p(
        "mosquitto",
        "Eclipse Mosquitto",
        "messaging",
        "Mosquitto MQTT broker: service active and listening.",
        ("mosquitto",),
        ("mosquitto",),
        _main(1883),
    ),
    _p(
        "emqx",
        "EMQX",
        "messaging",
        "EMQX MQTT broker: service, MQTT port and dashboard.",
        ("emqx",),
        (),
        (("PORT", 1883, "MQTT"), ("DASH_PORT", 18083, "dashboard")),
    ),
    _p(
        "vernemq",
        "VerneMQ",
        "messaging",
        "VerneMQ MQTT broker: service and MQTT port.",
        ("vernemq",),
        (),
        _main(1883),
    ),
    _p(
        "pulsar",
        "Apache Pulsar",
        "messaging",
        "Pulsar broker: service, broker port and HTTP admin.",
        ("pulsar*",),
        (),
        (("PORT", 6650, "broker"), ("HTTP_PORT", 8080, "HTTP admin")),
    ),
    _p(
        "redpanda",
        "Redpanda",
        "messaging",
        "Redpanda: service, Kafka API port and the admin API.",
        ("redpanda",),
        ("redpanda",),
        (("PORT", 9092, "Kafka API"), ("ADMIN_PORT", 9644, "admin")),
        auto_port=False,
    ),
    _p(
        "nsq",
        "NSQ",
        "messaging",
        "nsqd: service, TCP port and the /ping endpoint.",
        ("nsqd",),
        ("nsqd",),
        (("PORT", 4150, "TCP"), ("HTTP_PORT", 4151, "HTTP")),
        ("HTTP_PORT", "/ping", "200", "http"),
        auto_port=False,
    ),
    _p(
        "beanstalkd",
        "Beanstalkd",
        "messaging",
        "Beanstalkd work queue: service active and listening.",
        ("beanstalkd",),
        ("beanstalkd",),
        _main(11300),
    ),
    _p(
        "gearman",
        "Gearman",
        "messaging",
        "Gearman job server: service active and listening.",
        ("gearman*",),
        ("gearmand",),
        _main(4730),
    ),
    _p(
        "rocketmq",
        "Apache RocketMQ",
        "messaging",
        "RocketMQ name server: service and port.",
        ("rocketmq*", "rmq*"),
        (),
        _main(9876),
    ),
]

# ------------------------------------------------------------------------------ containers
CONTAINERS = [
    _p(
        "docker",
        "Docker Engine",
        "container",
        "Docker daemon and the runtime under it. The containers are in Containers.",
        ("docker", "containerd"),
        ("dockerd", "containerd"),
    ),
    _p(
        "containerd",
        "containerd",
        "container",
        "containerd runtime: service active.",
        ("containerd",),
        ("containerd",),
    ),
    _p(
        "podman",
        "Podman",
        "container",
        "Podman: its API socket service, when enabled.",
        ("podman*",),
        ("podman",),
    ),
    _p(
        "cri-o",
        "CRI-O",
        "container",
        "CRI-O container runtime: service active.",
        ("crio",),
        ("crio",),
    ),
    _p(
        "kubelet",
        "Kubernetes kubelet",
        "container",
        "kubelet: service active and its API port listening.",
        ("kubelet",),
        ("kubelet",),
        (("PORT", 10250, "kubelet API"),),
    ),
    _p(
        "kube-apiserver",
        "Kubernetes API server",
        "container",
        "kube-apiserver: process and secure port (control-plane nodes).",
        (),
        ("kube-apiserver",),
        (("PORT", 6443, "API"),),
    ),
    _p(
        "kube-proxy",
        "Kubernetes kube-proxy",
        "container",
        "kube-proxy: process running.",
        ("kube-proxy",),
        ("kube-proxy",),
    ),
    _p(
        "k3s",
        "K3s server",
        "container",
        "K3s server: service and API port.",
        ("k3s",),
        ("k3s-server", "k3s"),
        (("PORT", 6443, "API"),),
    ),
    _p(
        "k3s-agent",
        "K3s agent",
        "container",
        "K3s worker node: service active.",
        ("k3s-agent",),
        ("k3s-agent",),
    ),
    _p(
        "rke2",
        "RKE2",
        "container",
        "RKE2 server: service, API and supervisor ports.",
        ("rke2-server",),
        ("rke2",),
        (("PORT", 6443, "API"), ("SUP_PORT", 9345, "supervisor")),
        auto_port=False,
    ),
    _p(
        "microk8s",
        "MicroK8s",
        "container",
        "MicroK8s: its snap services.",
        ("snap.microk8s.daemon-kubelite", "snap.microk8s.daemon-containerd"),
        ("kubelite",),
    ),
    _p(
        "nomad",
        "HashiCorp Nomad",
        "container",
        "Nomad agent: service, HTTP port and the leader endpoint.",
        ("nomad",),
        ("nomad",),
        (("PORT", 4646, "HTTP"),),
        ("PORT", "/v1/status/leader", "200", "http"),
    ),
    _p(
        "lxd",
        "LXD",
        "container",
        "LXD: service (snap or package).",
        ("lxd*", "snap.lxd.daemon"),
        ("lxd",),
    ),
    _p("incus", "Incus", "container", "Incus: service active.", ("incus",), ("incusd",)),
    _p(
        "cadvisor",
        "cAdvisor",
        "container",
        "cAdvisor: service, port and the /healthz endpoint.",
        ("cadvisor",),
        ("cadvisor",),
        (("PORT", 8080, "HTTP"),),
        ("PORT", "/healthz", "200", "http"),
    ),
    _p(
        "docker-registry",
        "Docker Registry",
        "container",
        "Distribution registry: service, port and the /v2/ endpoint (401 counts).",
        ("docker-registry", "registry"),
        ("registry", "docker-registry"),
        _main(5000),
        ("PORT", "/v2/", "200-401", "http"),
    ),
    _p(
        "buildkit",
        "BuildKit",
        "container",
        "buildkitd: service active.",
        ("buildkit*",),
        ("buildkitd",),
    ),
]

# ------------------------------------------------------------------------------ monitoring
MONITORING = [
    _p(
        "prometheus",
        "Prometheus",
        "monitoring",
        "Prometheus: service, port and the /-/healthy endpoint.",
        ("prometheus",),
        ("prometheus",),
        _main(9090),
        ("PORT", "/-/healthy", "200", "http"),
    ),
    _p(
        "alertmanager",
        "Alertmanager",
        "monitoring",
        "Alertmanager: service, port and the /-/healthy endpoint.",
        ("alertmanager", "prometheus-alertmanager"),
        ("alertmanager",),
        _main(9093),
        ("PORT", "/-/healthy", "200", "http"),
    ),
    _p(
        "grafana",
        "Grafana",
        "monitoring",
        "Grafana: service, port and the /api/health endpoint.",
        ("grafana-server", "grafana"),
        ("grafana-server", "grafana"),
        _main(3000),
        ("PORT", "/api/health", "200", "http"),
    ),
    _p(
        "node-exporter",
        "Prometheus node_exporter",
        "monitoring",
        "node_exporter: service, port and metrics endpoint.",
        ("node_exporter", "node-exporter", "prometheus-node-exporter"),
        ("node_exporter",),
        _main(9100),
        ("PORT", "/metrics", "200", "http"),
    ),
    _p(
        "blackbox-exporter",
        "Blackbox exporter",
        "monitoring",
        "blackbox_exporter: service and port.",
        ("blackbox_exporter", "prometheus-blackbox-exporter"),
        ("blackbox_export",),
        _main(9115),
    ),
    _p(
        "loki",
        "Grafana Loki",
        "monitoring",
        "Loki: service, port and the /ready endpoint.",
        ("loki",),
        ("loki",),
        _main(3100),
        ("PORT", "/ready", "200", "http"),
    ),
    _p(
        "promtail",
        "Promtail",
        "monitoring",
        "Promtail: service, port and the /ready endpoint.",
        ("promtail",),
        ("promtail",),
        _main(9080),
        ("PORT", "/ready", "200", "http"),
    ),
    _p(
        "tempo",
        "Grafana Tempo",
        "monitoring",
        "Tempo: service, port and the /ready endpoint.",
        ("tempo",),
        ("tempo",),
        _main(3200),
        ("PORT", "/ready", "200", "http"),
    ),
    _p(
        "victoriametrics",
        "VictoriaMetrics",
        "monitoring",
        "VictoriaMetrics: service, port and the /health endpoint.",
        ("victoria-metrics", "victoriametrics"),
        ("victoria-metric",),
        _main(8428),
        ("PORT", "/health", "200", "http"),
    ),
    _p(
        "zabbix-server",
        "Zabbix server",
        "monitoring",
        "Zabbix server: service and trapper port.",
        ("zabbix-server",),
        ("zabbix_server",),
        _main(10051),
    ),
    _p(
        "zabbix-agent",
        "Zabbix agent",
        "monitoring",
        "Zabbix agent (1 or 2): service and passive port.",
        ("zabbix-agent*",),
        ("zabbix_agentd", "zabbix_agent2"),
        _main(10050),
    ),
    _p(
        "nagios",
        "Nagios Core",
        "monitoring",
        "Nagios Core: service active.",
        ("nagios*",),
        ("nagios",),
    ),
    _p(
        "icinga2",
        "Icinga 2",
        "monitoring",
        "Icinga 2: service and API port.",
        ("icinga2",),
        ("icinga2",),
        _main(5665),
    ),
    _p(
        "checkmk-agent",
        "Checkmk agent",
        "monitoring",
        "Checkmk agent: its socket and port 6556.",
        ("check-mk-agent*", "cmk-agent-ctl*"),
        ("cmk-agent-ctl",),
        _main(6556),
    ),
    _p(
        "netdata",
        "Netdata",
        "monitoring",
        "Netdata: service, port and the /api/v1/info endpoint.",
        ("netdata",),
        ("netdata",),
        _main(19999),
        ("PORT", "/api/v1/info", "200", "http"),
    ),
    _p(
        "telegraf",
        "Telegraf",
        "monitoring",
        "Telegraf: service active.",
        ("telegraf",),
        ("telegraf",),
    ),
    _p(
        "kibana",
        "Kibana",
        "monitoring",
        "Kibana: service, port and the status API (401 counts).",
        ("kibana",),
        (),
        _main(5601),
        ("PORT", "/api/status", "200-401", "http"),
    ),
    _p(
        "logstash",
        "Logstash",
        "monitoring",
        "Logstash: service and monitoring API.",
        ("logstash",),
        (),
        (("PORT", 9600, "monitoring API"),),
        ("PORT", "/", "200", "http"),
    ),
    _p(
        "fluentd",
        "Fluentd / td-agent",
        "monitoring",
        "Fluentd: service and forward port.",
        ("fluentd", "td-agent"),
        (),
        _main(24224),
    ),
    _p(
        "fluent-bit",
        "Fluent Bit",
        "monitoring",
        "Fluent Bit: service and its HTTP server.",
        ("fluent-bit",),
        ("fluent-bit",),
        (("PORT", 2020, "HTTP"),),
        ("PORT", "/", "200", "http"),
    ),
    _p(
        "graylog",
        "Graylog",
        "monitoring",
        "Graylog: service and web port.",
        ("graylog-server",),
        (),
        _main(9000),
    ),
    _p(
        "otel-collector",
        "OpenTelemetry Collector",
        "monitoring",
        "Collector: service and health_check extension.",
        ("otelcol*", "opentelemetry-collector*"),
        ("otelcol", "otelcol-contrib"),
        (("PORT", 13133, "health"),),
        ("PORT", "/", "200", "http"),
    ),
]

# ------------------------------------------------------------------------------ mail
MAIL = [
    _p(
        "postfix",
        "Postfix",
        "mail",
        "Postfix: service and SMTP port.",
        ("postfix",),
        (),
        _main(25),
        auto_port=False,
        detect_ports=(),
    ),
    _p(
        "exim",
        "Exim",
        "mail",
        "Exim: service and SMTP port.",
        ("exim4", "exim"),
        ("exim4", "exim"),
        _main(25),
    ),
    _p(
        "sendmail",
        "Sendmail",
        "mail",
        "Sendmail: service and SMTP port.",
        ("sendmail",),
        ("sendmail",),
        _main(25),
    ),
    _p(
        "opensmtpd",
        "OpenSMTPD",
        "mail",
        "OpenSMTPD: service and SMTP port.",
        ("opensmtpd", "smtpd"),
        (),
        _main(25),
    ),
    _p(
        "dovecot",
        "Dovecot",
        "mail",
        "Dovecot: service, IMAP and IMAPS ports.",
        ("dovecot",),
        ("dovecot",),
        (("PORT", 143, "IMAP"), ("TLS_PORT", 993, "IMAPS")),
        auto_port=False,
    ),
    _p(
        "cyrus-imapd",
        "Cyrus IMAP",
        "mail",
        "Cyrus IMAP: service and IMAP port.",
        ("cyrus-imapd",),
        ("cyrmaster",),
        _main(143),
    ),
    _p(
        "opendkim",
        "OpenDKIM",
        "mail",
        "OpenDKIM: service and milter port.",
        ("opendkim",),
        ("opendkim",),
        _main(8891),
    ),
    _p(
        "opendmarc",
        "OpenDMARC",
        "mail",
        "OpenDMARC: service active.",
        ("opendmarc",),
        ("opendmarc",),
    ),
    _p(
        "spamassassin",
        "SpamAssassin",
        "mail",
        "spamd: service and port.",
        ("spamassassin", "spamd"),
        ("spamd",),
        _main(783),
    ),
    _p(
        "rspamd",
        "Rspamd",
        "mail",
        "Rspamd: service, milter port and the controller /ping.",
        ("rspamd",),
        ("rspamd",),
        (("PORT", 11332, "milter"), ("WEB_PORT", 11334, "controller")),
        ("WEB_PORT", "/ping", "200", "http"),
        auto_port=False,
    ),
    _p(
        "clamav",
        "ClamAV",
        "mail",
        "clamd: service and TCP port (when it listens on TCP).",
        ("clamav-daemon", "clamd*"),
        ("clamd",),
        _main(3310),
    ),
    _p(
        "amavis",
        "Amavis",
        "mail",
        "amavisd: service and port.",
        ("amavis*",),
        ("amavisd",),
        _main(10024),
    ),
    _p(
        "postgrey",
        "Postgrey",
        "mail",
        "Postgrey greylisting: service and port.",
        ("postgrey",),
        ("postgrey",),
        _main(10023),
    ),
    _p(
        "mailpit",
        "Mailpit / MailHog",
        "mail",
        "Mail catcher: service, SMTP port and web UI.",
        ("mailpit", "mailhog"),
        ("mailpit", "MailHog"),
        (("PORT", 1025, "SMTP"), ("WEB_PORT", 8025, "web UI")),
        ("WEB_PORT", "/", "200", "http"),
        auto_port=False,
    ),
    _p(
        "stalwart",
        "Stalwart Mail",
        "mail",
        "Stalwart mail server: service and SMTP port.",
        ("stalwart-mail",),
        ("stalwart-mail",),
        _main(25),
    ),
]

# ------------------------------------------------------------------------------ infrastructure
INFRA = [
    _p(
        "bind9",
        "BIND 9",
        "infra",
        "BIND name server: service and DNS port.",
        ("named", "bind9"),
        ("named",),
        _main(53),
        auto_port=False,
    ),
    _p(
        "unbound",
        "Unbound",
        "infra",
        "Unbound resolver: service and DNS port.",
        ("unbound",),
        ("unbound",),
        _main(53),
        auto_port=False,
    ),
    _p(
        "dnsmasq",
        "dnsmasq",
        "infra",
        "dnsmasq (DNS and DHCP): service and DNS port.",
        ("dnsmasq",),
        ("dnsmasq",),
        _main(53),
        auto_port=False,
    ),
    _p(
        "powerdns",
        "PowerDNS Authoritative",
        "infra",
        "pdns: service and DNS port.",
        ("pdns",),
        ("pdns_server",),
        _main(53),
        auto_port=False,
    ),
    _p(
        "pdns-recursor",
        "PowerDNS Recursor",
        "infra",
        "pdns-recursor: service and DNS port.",
        ("pdns-recursor",),
        ("pdns_recursor",),
        _main(53),
        auto_port=False,
    ),
    _p(
        "coredns",
        "CoreDNS",
        "infra",
        "CoreDNS: service, DNS port and the metrics port.",
        ("coredns",),
        ("coredns",),
        (("PORT", 53, "DNS"), ("METRICS_PORT", 9153, "metrics")),
        auto_port=False,
    ),
    _p(
        "knot",
        "Knot DNS",
        "infra",
        "Knot DNS: service and DNS port.",
        ("knot",),
        ("knotd",),
        _main(53),
        auto_port=False,
    ),
    _p(
        "isc-dhcp",
        "ISC DHCP server",
        "infra",
        "dhcpd: service active (DHCP is UDP: no port check).",
        ("isc-dhcp-server", "dhcpd"),
        ("dhcpd",),
    ),
    _p(
        "kea",
        "Kea DHCP",
        "infra",
        "Kea DHCPv4: service active.",
        ("kea-dhcp4*", "isc-kea-dhcp4*"),
        ("kea-dhcp4",),
    ),
    _p(
        "openldap",
        "OpenLDAP",
        "infra",
        "slapd: service, LDAP and LDAPS ports.",
        ("slapd",),
        ("slapd",),
        (("PORT", 389, "LDAP"), ("TLS_PORT", 636, "LDAPS")),
        auto_port=False,
    ),
    _p(
        "389ds",
        "389 Directory Server",
        "infra",
        "389-ds: service and LDAP port.",
        ("dirsrv*",),
        ("ns-slapd",),
        _main(389),
    ),
    _p(
        "keycloak",
        "Keycloak",
        "infra",
        "Keycloak: service and HTTP port.",
        ("keycloak",),
        (),
        (("PORT", 8080, "HTTP"),),
        ("PORT", "/", "200-399", "http"),
    ),
    _p(
        "openvpn",
        "OpenVPN",
        "infra",
        "OpenVPN: service active (UDP by default: no port check).",
        ("openvpn*",),
        ("openvpn",),
    ),
    _p("wireguard", "WireGuard", "infra", "wg-quick interface unit active.", ("wg-quick@*",)),
    _p(
        "strongswan",
        "strongSwan (IPsec)",
        "infra",
        "strongSwan: service active.",
        ("strongswan*", "ipsec"),
        ("charon",),
    ),
    _p(
        "chrony",
        "chrony",
        "infra",
        "chrony NTP: service active.",
        ("chrony", "chronyd"),
        ("chronyd",),
    ),
    _p("ntpd", "ntpd", "infra", "ntpd: service active.", ("ntp", "ntpd"), ("ntpd",)),
    _p(
        "samba",
        "Samba",
        "infra",
        "smbd: service and SMB port.",
        ("smbd", "smb"),
        ("smbd",),
        _main(445),
        detect_ports=(445,),
    ),
    _p(
        "nfs-server",
        "NFS server",
        "infra",
        "NFS server: service and port 2049.",
        ("nfs-server", "nfs-kernel-server"),
        (),
        _main(2049),
    ),
    _p(
        "vsftpd",
        "vsftpd",
        "infra",
        "vsftpd: service and FTP port.",
        ("vsftpd",),
        ("vsftpd",),
        _main(21),
    ),
    _p(
        "proftpd",
        "ProFTPD",
        "infra",
        "ProFTPD: service and FTP port.",
        ("proftpd",),
        ("proftpd",),
        _main(21),
    ),
    _p(
        "minio",
        "MinIO",
        "infra",
        "MinIO: service, port and the live health endpoint.",
        ("minio",),
        ("minio",),
        _main(9000),
        ("PORT", "/minio/health/live", "200", "http"),
    ),
    _p(
        "vault",
        "HashiCorp Vault / OpenBao",
        "infra",
        "Vault or OpenBao: service, port and /v1/sys/health (sealed or standby are accepted).",
        ("vault", "openbao"),
        ("vault", "bao"),
        _main(8200),
        ("PORT", "/v1/sys/health", "200-599", "https"),
    ),
    _p(
        "consul",
        "HashiCorp Consul",
        "infra",
        "Consul agent: service, HTTP port and the leader endpoint.",
        ("consul",),
        ("consul",),
        (("PORT", 8500, "HTTP"),),
        ("PORT", "/v1/status/leader", "200", "http"),
    ),
    _p(
        "fail2ban",
        "Fail2ban",
        "infra",
        "fail2ban: service active.",
        ("fail2ban",),
        ("fail2ban-server",),
    ),
    _p(
        "crowdsec",
        "CrowdSec",
        "infra",
        "CrowdSec: service and local API port.",
        ("crowdsec",),
        ("crowdsec",),
        (("PORT", 8080, "local API"),),
    ),
    _p("rsyslog", "rsyslog", "infra", "rsyslog: service active.", ("rsyslog",), ("rsyslogd",)),
    _p(
        "tailscale",
        "Tailscale",
        "infra",
        "tailscaled: service active.",
        ("tailscaled",),
        ("tailscaled",),
    ),
    _p(
        "zerotier",
        "ZeroTier",
        "infra",
        "ZeroTier One: service active.",
        ("zerotier-one",),
        ("zerotier-one",),
    ),
]

# ------------------------------------------------------------------------------ devops
DEVOPS = [
    _p(
        "jenkins",
        "Jenkins",
        "devops",
        "Jenkins: service, port and the login page.",
        ("jenkins",),
        (),
        (("PORT", 8080, "HTTP"),),
        ("PORT", "/login", "200", "http"),
    ),
    _p(
        "gitea",
        "Gitea",
        "devops",
        "Gitea: service, port and the health endpoint.",
        ("gitea",),
        ("gitea",),
        _main(3000),
        ("PORT", "/api/healthz", "200", "http"),
    ),
    _p(
        "forgejo",
        "Forgejo",
        "devops",
        "Forgejo: service, port and the health endpoint.",
        ("forgejo",),
        ("forgejo",),
        _main(3000),
        ("PORT", "/api/healthz", "200", "http"),
    ),
    _p(
        "gitlab",
        "GitLab",
        "devops",
        "GitLab (Omnibus): its supervisor service and web port.",
        ("gitlab-runsvdir",),
        (),
        (("PORT", 80, "HTTP"),),
        auto_port=False,
    ),
    _p(
        "gitlab-runner",
        "GitLab Runner",
        "devops",
        "gitlab-runner: service active.",
        ("gitlab-runner",),
        ("gitlab-runner",),
    ),
    _p(
        "github-runner",
        "GitHub Actions runner",
        "devops",
        "Self-hosted runner service.",
        ("actions.runner.*",),
        ("Runner.Listener",),
    ),
    _p(
        "woodpecker",
        "Woodpecker CI",
        "devops",
        "Woodpecker server: service and port.",
        ("woodpecker-server",),
        ("woodpecker-serv",),
        (("PORT", 8000, "HTTP"),),
        ("PORT", "/healthz", "200", "http"),
    ),
    _p(
        "teamcity",
        "TeamCity",
        "devops",
        "TeamCity server: service and port.",
        ("teamcity*",),
        (),
        (("PORT", 8111, "HTTP"),),
    ),
    _p(
        "nexus",
        "Sonatype Nexus Repository",
        "devops",
        "Nexus: service, port and the status endpoint.",
        ("nexus*",),
        (),
        (("PORT", 8081, "HTTP"),),
        ("PORT", "/service/rest/v1/status", "200", "http"),
    ),
    _p(
        "artifactory",
        "JFrog Artifactory",
        "devops",
        "Artifactory: service, router port and its health endpoint.",
        ("artifactory",),
        (),
        (("PORT", 8082, "router"),),
        ("PORT", "/router/api/v1/system/health", "200", "http"),
    ),
    _p(
        "sonarqube",
        "SonarQube",
        "devops",
        "SonarQube: service, port and the status endpoint.",
        ("sonarqube", "sonar"),
        (),
        (("PORT", 9000, "HTTP"),),
        ("PORT", "/api/system/status", "200", "http"),
    ),
    _p(
        "harbor",
        "Harbor",
        "devops",
        "Harbor registry (docker-compose install): its port and health API.",
        ("harbor*",),
        (),
        (("PORT", 80, "HTTP"),),
        ("PORT", "/api/v2.0/health", "200", "http"),
    ),
]

PRODUCTS: list[Product] = [
    *DATABASES,
    *WEB,
    *MESSAGING,
    *CONTAINERS,
    *MONITORING,
    *MAIL,
    *INFRA,
    *DEVOPS,
]


def _port_name(product: Product, port: Port, first: bool) -> str:
    macro, _, what = port
    return (
        f"{product.name} listening on {{${macro}}}"
        if first
        else f"{product.name} {what} on {{${macro}}}"
    )


# Macros the agent fills from the configuration it reads (agent: services_config_unix.go):
# (product, macro) -> the setting of the service. An agent that does not report it: the default.
CONFIG_AUTO: dict[tuple[str, str], str] = {
    ("nginx", "HTTP_PORT"): "http_ports",
    ("nginx", "HTTPS_PORT"): "https_ports",
}


def template(product: Product) -> dict[str, Any]:
    """The template of one product, in the shape of a `builtin_templates/*.json` file."""
    macros: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    unit_macro = "UNIT"
    if product.units:
        first = product.units[0].replace("*", "")
        macros.append(
            {
                "name": unit_macro,
                "default": first,
                "description": "systemd unit (distributions name it differently)",
                "auto": {
                    "from": "unit",
                    "pattern": [f"{u}*" if "*" not in u else u for u in product.units],
                },
            }
        )
        items.append(
            {
                "name": f"{product.name} service",
                "kind": "service",
                "params": {"unit": f"{{${unit_macro}}}"},
                "check_cycles": 2,
            }
        )
    for index, port in enumerate(product.ports):
        macro, default, what = port
        entry: dict[str, Any] = {
            "name": macro,
            "default": default,
            "description": f"TCP port ({what})",
        }
        if (product.slug, macro) in CONFIG_AUTO:
            entry["auto"] = {
                "from": "config",
                "service": product.slug,
                "key": CONFIG_AUTO[(product.slug, macro)],
                "pick": "min",
            }
        elif index == 0 and product.auto_port and product.procs:
            entry["auto"] = {
                "from": "listening_port",
                "process": list(product.procs),
                "pick": "min",
            }
        macros.append(entry)
        items.append(
            {
                "name": _port_name(product, port, index == 0),
                "kind": "port",
                "params": {"port": f"{{${macro}}}"},
                "check_cycles": 2,
            }
        )
    if product.http:
        macro, path, status, scheme = product.http
        macros.append(
            {"name": "HTTP_PATH", "default": path, "description": "URL path of the health check"}
        )
        macros.append(
            {"name": "HTTP_STATUS", "default": status, "description": "Accepted HTTP status codes"}
        )
        params: dict[str, Any] = {
            "url": f"{scheme}://{{$NODE_ADDRESS}}:{{${macro}}}{{$HTTP_PATH}}",
            "expect_status": "{$HTTP_STATUS}",
        }
        if scheme == "https":
            macros.append(
                {
                    "name": "VERIFY_TLS",
                    "default": False,
                    "description": "Verify the TLS certificate",
                }
            )
            params["verify_tls"] = "{$VERIFY_TLS}"
        items.append(
            {
                "name": f"{product.name} answers HTTP",
                "kind": "http",
                "params": params,
                "check_cycles": 2,
            }
        )
    # the same reads from the server, through the service the node has of this kind
    # (Inventory → Services): they say "unknown" until that service is added
    if product.ports:
        items.append(
            {
                "name": f"{product.name} TCP latency (service)",
                "kind": "probe",
                "params": {"probe": "service_tcp.latency_ms", "args": {"kind": product.slug}},
                "comparison": "gt",
                "warn": 500,
                "crit": 2000,
                "check_cycles": 2,
            }
        )
    if product.http:
        items.append(
            {
                "name": f"{product.name} health latency (service)",
                "kind": "probe",
                "params": {"probe": "service_http.latency_ms", "args": {"kind": product.slug}},
                "comparison": "gt",
                "warn": 1000,
                "crit": 5000,
                "check_cycles": 2,
            }
        )
    ports = (
        list(product.detect_ports)
        if product.detect_ports is not None
        else [p[1] for p in product.ports if p[1] not in GENERIC_PORTS]
    )
    return {
        "slug": product.slug,
        "version": 2,
        "name": f"{product.name} (service)",
        "category": product.category,
        "description": product.description[:500],
        "requires": "agent",
        "macros": macros,
        "detect": {
            "processes": list(product.procs),
            "ports": ports,
            "units": [f"{u}*" if "*" not in u else u for u in product.units],
        },
        "items": items,
    }


def templates() -> list[dict[str, Any]]:
    return [template(p) for p in PRODUCTS]
