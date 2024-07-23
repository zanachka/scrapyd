import datetime
import multiprocessing
import sys
from itertools import chain

from twisted.application.service import Service
from twisted.internet import defer, error, protocol, reactor
from twisted.logger import Logger

from scrapyd import __version__
from scrapyd.interfaces import IEnvironment, IJobStorage, IPoller
from scrapyd.utils import native_stringify_dict

log = Logger()


def get_crawl_args(message):
    """Return the command-line arguments to use for the scrapy crawl process
    that will be started for this message
    """
    copied = message.copy()
    del copied["_project"]

    return [
        copied.pop("_spider"),
        *chain.from_iterable(["-s", f"{key}={value}"] for key, value in copied.pop("settings", {}).items()),
        *chain.from_iterable(["-a", f"{key}={value}"] for key, value in copied.items()),  # spider arguments
    ]


class Launcher(Service):
    name = "launcher"

    def __init__(self, config, app):
        self.processes = {}
        self.finished = app.getComponent(IJobStorage)
        self.max_proc = self._get_max_proc(config)
        self.runner = config.get("runner", "scrapyd.runner")
        self.app = app

    def startService(self):
        for slot in range(self.max_proc):
            self._get_message(slot)
        log.info(
            "Scrapyd {version} started: max_proc={max_proc!r}, runner={runner!r}",
            version=__version__,
            max_proc=self.max_proc,
            runner=self.runner,
            log_system="Launcher",
        )

    def _get_message(self, slot):
        poller = self.app.getComponent(IPoller)
        poller.next().addCallback(self._spawn_process, slot)

    def _spawn_process(self, message, slot):
        environ = self.app.getComponent(IEnvironment)
        message.setdefault("settings", {})
        message["settings"].update(environ.get_settings(message))
        decoded = native_stringify_dict(message)
        project = decoded["_project"]

        args = [sys.executable, "-m", self.runner, "crawl"]
        args += get_crawl_args(decoded)

        env = environ.get_environment(decoded, slot)
        env = native_stringify_dict(env)

        process = ScrapyProcessProtocol(project, decoded["_spider"], decoded["_job"], env, args)
        process.deferred.addBoth(self._process_finished, slot)

        reactor.spawnProcess(process, sys.executable, args=args, env=env)
        self.processes[slot] = process

    def _process_finished(self, _, slot):
        process = self.processes.pop(slot)
        process.end_time = datetime.datetime.now()
        self.finished.add(process)
        self._get_message(slot)

    def _get_max_proc(self, config):
        max_proc = config.getint("max_proc", 0)
        if max_proc:
            return max_proc

        try:
            cpus = multiprocessing.cpu_count()
        except NotImplementedError:  # Windows 17520a3
            cpus = 1
        return cpus * config.getint("max_proc_per_cpu", 4)


# https://docs.twisted.org/en/stable/api/twisted.internet.protocol.ProcessProtocol.html
class ScrapyProcessProtocol(protocol.ProcessProtocol):
    def __init__(self, project, spider, job, env, args):
        self.pid = None
        self.project = project
        self.spider = spider
        self.job = job
        self.start_time = datetime.datetime.now()
        self.end_time = None
        self.env = env
        self.args = args
        self.deferred = defer.Deferred()

    def __repr__(self):
        return (
            f"ScrapyProcessProtocol(pid={self.pid} project={self.project} spider={self.spider} job={self.job} "
            f"start_time={self.start_time} end_time={self.end_time} env={self.env} args={self.args})"
        )

    def outReceived(self, data):
        log.info(data.rstrip(), log_system=f"Launcher,{self.pid}/stdout")

    def errReceived(self, data):
        log.error(data.rstrip(), log_system=f"Launcher,{self.pid}/stderr")

    def connectionMade(self):
        self.pid = self.transport.pid
        self.log("info", "Process started:")

    # https://docs.twisted.org/en/stable/core/howto/process.html#things-that-can-happen-to-your-processprotocol
    def processEnded(self, status):
        if isinstance(status.value, error.ProcessDone):
            self.log("info", "Process finished:")
        else:
            self.log("error", f"Process died: exitstatus={status.value.exitCode!r}")
        self.deferred.callback(self)

    def asdict(self):
        return {
            "project": self.project,
            "spider": self.spider,
            "id": self.job,
            "pid": self.pid,
            "start_time": str(self.start_time),
        }

    def log(self, level, action):
        getattr(log, level)(
            "{action} project={project!r} spider={spider!r} job={job!r} pid={pid!r} args={args!r}",
            action=action,
            project=self.project,
            spider=self.spider,
            job=self.job,
            pid=self.pid,
            args=self.args,
        )
