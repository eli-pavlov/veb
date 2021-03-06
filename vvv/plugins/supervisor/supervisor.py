import logging
import os
import subprocess
import time
from jadi import component

from vvv.api.app import AppType
from vvv.api.aug import Augeas
from vvv.api.config import MainConfig, SystemConfig
from vvv.api.configurable import Configurable
from vvv.api.check import Check, CheckFailure
from vvv.api.restartable import Restartable
from vvv.api.util import absolute_path

from .plugin import SupervisorImpl


@component(Check)
class AppsCheck(Check):
    def get_instances(self):
        for website in MainConfig.get(self.context).data['websites']:
            if website['enabled']:
                prefix = 'veb-app-%s-' % website['name']
                for app in website['apps']:
                    app_type = AppType.by_name(self.context, app['type'])
                    if not app_type:
                        logging.warn('Skipping unknown app type "%s"', app['type'])
                        continue
                    process_info = app_type.get_process(website, app)
                    if process_info:
                        full_name = prefix + app['name'] + process_info.get('suffix', '')
                        yield [{
                            'full_name': full_name,
                            'website': website,
                            'app': app,
                        }]

    def get_name(self, info):
        return 'app %s/%s is running' % (
            info['website']['name'],
            info['app']['name'],
        )

    def run(self, info):
        o = subprocess.check_output(
            ['supervisorctl', 'status', info['full_name']]
        )
        if 'RUNNING' not in o:
            raise CheckFailure(o)
        return True


@component(Check)
class SupervisorServiceCheck(Check):
    name = 'supervisor is running'

    def run(self):
        return subprocess.call(
            ['service', SupervisorImpl.any(self.context).service_name, 'status']
        ) == 0


@component(AppType)
class GenericAppType(AppType):
    name = 'generic'

    def get_access_type(self, website, app):
        return None

    def get_access_params(self, website, app):
        return {}

    def get_process(self, website, app):
        cmd = app['params']['command']
        if cmd.startswith('./'):
            if app['path']:
                cmd = os.path.join(absolute_path(app['path'], website['root']), cmd[2:])
            else:
                cmd = os.path.join(website['root'], cmd[2:])
        return {
            'command': cmd,
            'directory': app['path'] or website['root'],
            'environment': app['params']['environment'],
            'user': app['params']['user'],
            'autorestart': app['params']['autorestart'],
            'startretries': app['params']['startretries'],
        }


@component(Configurable)
class Supervisor(Configurable):
    name = 'supervisor'

    def configure(self):
        aug = Augeas(
            modules=[{
                'name': 'Supervisor',
                'lens': 'Supervisor.lns',
                'incl': [
                    SystemConfig.get(self.context).data['supervisor']['config_file'],
                ]
            }],
            loadpath=os.path.dirname(__file__),
        )
        aug_path = '/files' + SystemConfig.get(self.context).data['supervisor']['config_file']
        aug.load()

        for website in MainConfig.get(self.context).data['websites']:
            if website['enabled'] and not website['maintenance_mode']:
                prefix = 'veb-app-%s-' % website['name']

                for path in aug.match(aug_path + '/*'):
                    if aug.get(path + '/#titlecomment') == 'Autogenerated Ajenti V process':
                        aug.remove(path)
                    if aug.get(path + '/#titlecomment') == 'Generated by Ajenti-V':
                        aug.remove(path)
                    if prefix in path:
                        aug.remove(path)

                for app in website['apps']:
                    app_type = AppType.by_name(self.context, app['type'])
                    if not app_type:
                        logging.warn('Skipping unknown app type "%s"', app['type'])
                        continue

                    process_info = app_type.get_process(website, app)
                    if process_info:
                        full_name = prefix + app['name'] + process_info.get('suffix', '')
                        path = aug_path + '/program:%s' % full_name
                        aug.set(path + '/command', process_info['command'])
                        aug.set(
                            path + '/directory',
                            absolute_path(process_info['directory'], website['root']) or website['root']
                        )
                        if process_info['environment']:
                            aug.set(path + '/environment', process_info['environment'])
                        aug.set(path + '/user', process_info['user'])
                        aug.set(path + '/killasgroup', 'true')
                        aug.set(path + '/stopasgroup', 'true')
                        aug.set(path + '/startsecs', str(process_info.get('startsecs', 1)))
                        aug.set(path + '/startretries', str(process_info.get('startretries', 5)))
                        aug.set(path + '/autorestart', str(process_info.get('autorestart', 'unexpected')).lower())
                        aug.set(path + '/stdout_logfile', '%s/%s/%s.stdout.log' % (
                            SystemConfig.get(self.context).data['log_dir'],
                            website['name'],
                            app['name'],
                        ))
                        aug.set(path + '/stderr_logfile', '%s/%s/%s.stderr.log' % (
                            SystemConfig.get(self.context).data['log_dir'],
                            website['name'],
                            app['name'],
                        ))

        aug.save()
        SupervisorRestartable.get(self.context).schedule_restart()


@component(Restartable)
class SupervisorRestartable(Restartable):
    name = 'supervisor'

    def do_restart(self):
        subprocess.call(['service', SupervisorImpl.any(self.context).service_name, 'start'])
        time.sleep(2)
        subprocess.call(['supervisorctl', 'reload'])
