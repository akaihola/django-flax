# pylint: disable=W0142
#         (* and ** magic)

from collections import defaultdict
from contextlib import contextmanager as _contextmanager
from fabric.api import env, local, run, sudo, task
from fabric.context_managers import cd, prefix, settings
from fabric.contrib.files import append, comment, upload_template
from fabric.operations import put
import logging
import os
from tempfile import NamedTemporaryFile


logger = logging.getLogger('flax')

here = lambda *parts: os.path.join(os.path.dirname(__file__), *parts)


env.debs_by_roledef = defaultdict(
    set,
    {'dbserver': set(['postgresql']),
     'webserver': set(['nginx']),
     'appserver': set(['git',
                       'python',
                       'python-psycopg2',
                       'python-virtualenv',
                       'subversion',
                       'supervisor'])})


env.branch = 'master'


class Pip(object):
    def run(self, args):
        run('pip {0}'.format(args))

    def install(self, args):
        with virtualenv():
            self.run('install'
                     ' --download-cache=~/.pip/cache'
                     ' {0}'.format(args))

    def install_repo(self, repository):
        self.install(' -I -e {0}'.format(repository))

    def update_repo(self, repository):
        self.install(' -U -e {0}'.format(repository))

    def update_requirements(self, requirements_filepath):
        self.install('-U -r {0}'.format(requirements_filepath))


pip = Pip()


@task
def bootstrap():
    install_debs()
    install_project()
    configure_postgresql()
    configure_supervisor()


@task
def create_db_user():
    with settings(warn_only=True):
        sudo('createuser -DRSw {db_user}'.format(**env), user='postgres')
    sudo('psql -c'
         ' "ALTER USER {db_user} PASSWORD \'{db_password}\';"'.format(**env),
         user='postgres')


@task
def configure_postgresql():
    pg_hba = '/etc/postgresql/8.4/main/pg_hba.conf'
    params = {'db_name': getattr(env, 'db_name', env.project_name),
              'db_user': getattr(env, 'db_user', env.project_name)}
    append(pg_hba,
           'local {db_name} {db_user} password'.format(**params),
           use_sudo=True)
    comment(pg_hba,
            'local   all         all                               ident',
            use_sudo=True)
    sudo('service postgresql restart')


@task
def create_db():
    sudo('createdb {db_name} -O {db_user}'.format(**env), user='postgres')


@task
def clone_db():
    """Clones the production database to the development environment"""
    run('sudo -u postgres pg_dump -O {db_name}'
        ' >{site_root}/{db_name}.sql'.format(**env))
    local('rsync -z {host}:{site_root}/{db_name}.sql ./'.format(**env))
    with settings(warn_only=True):
        local('dropdb {db_name}'.format(**env))
        local('createuser -dRS {db_user}'.format(**env))
    local('createdb -O {db_user} {db_name}'.format(**env))
    local('psql -U {db_user} {db_name} <{db_name}.sql'.format(**env))


@task
def manage(*args):
    with virtualenv():
        run('manage {0} --settings={{django_settings_module}}'
            .format(' '.join(args)).format(**env))


@task
def collectstatic():
    manage('collectstatic', '--noinput')


def upload_configuration(filename,
                         destination,
                         template_dir=None,
                         context=None):
    tmpldir = template_dir or here('conf')
    upload_template(filename,
                    destination.format(**env),
                    use_jinja=True,
                    context=context or env,
                    template_dir=tmpldir,
                    use_sudo=True)


@task
def configure_nginx():
    upload_configuration('nginx-site.conf',
                         '/etc/nginx/sites-available/{project_name}')
    sudo('ln -sf'
         ' /etc/nginx/sites-available/{project_name}'
         ' /etc/nginx/sites-enabled/'.format(**env))
    for site in getattr(env, 'media_sites', ()):
        site['project_name'] = env.project_name
        upload_configuration('nginx-media.conf',
                             '/etc/nginx/sites-available/{name}'.format(**site),
                             context=site)
        sudo('ln -sf'
             ' /etc/nginx/sites-available/{name}'
             ' /etc/nginx/sites-enabled/'.format(**site))
    sudo('service nginx restart')


@task
def configure_supervisor():
    upload_configuration('supervisor-appserver.conf',
                         '/etc/supervisor/conf.d/{project_name}.conf')
    logdir = '/var/log/www/{project_name}'.format(**env)
    sudo('mkdir -p {0}'.format(logdir))
    sudo('chown www-data {0}'.format(logdir))
    with settings(warn_only=True):
        sudo('supervisorctl reload')


def get_roles():
    logger.debug('finding roles for %s in %s',
                 env.host, env.roledefs)
    roles = [role for role, hosts in env.roledefs.iteritems()
             if env.host in hosts]
    logger.debug('%s roles: %s', env.host, ', '.join(roles))
    return roles


def get_debs():
    return [debs
            for role in get_roles()
            for debs in env.debs_by_roledef[role]]


@task
def install_debs():
    debs = ' '.join(get_debs())
    sudo('apt-get install -y {0}'.format(debs))


def install_django():
    raise NotImplementedError


@task
def create_project_root():
    sudo('mkdir -p {project_root}'.format(**env))
    sudo('chown {user}.{user} {project_root}'.format(**env))


@task
def create_virtualenv():
    with cd(env.project_root):
        run('virtualenv --distribute .')


@task
def install_project():
    create_project_root()
    create_virtualenv()
    update_python_packages()


@task
def restart_django():
    """Restart Django processes"""
    # use full path to prevent password prompt if /usr/bin/supervisorctl is
    # specifically allowed in /etc/sudoers
    if env.webserver == 'gunicorn' and env.process_control == 'supervisor':
        sudo('/usr/bin/supervisorctl restart {project_name}'.format(**env),
             shell=False)
    elif env.webserver == 'apache' and env.process_control == 'sysvinit':
        sudo('/etc/init.d/apache2 restart')
    else:
        raise NotImplementedError('Unknown web server ({webserver})'
                                  ' and process controller ({process_control})'
                                  ' combination')


@_contextmanager
def virtualenv():
    """Context manager for activating the virtualenv

    From: http://stackoverflow.com/questions/1180411
    """
    with cd(env.project_root):
        with prefix('source {project_root}/bin/activate'.format(**env)):
            yield


def pull_repo():
    """Pull the newest revision of the main project repository"""
    with virtualenv():
        pip.install_repo(env.repository)


@task
def update_python_packages():
    """Update main project repository and its Python dependencies"""
    with NamedTemporaryFile() as tmp:
        if os.path.isfile('requirements/production.txt'):
            tmp.file.write(open('requirements/production.txt').read())
        tmp.file.write('-e '
                       'git+'
                       'ssh://{repository}'
                       '@{branch}'
                       '#egg={project_name}\n'.format(**env))
        tmp.file.flush()
        remote_name = (
            '/tmp/{project_name}.requirements.production.txt'.format(**env))
        put(tmp.name, remote_name)
        pip.update_requirements(remote_name)
        run('rm {0}'.format(remote_name))


@task
def update_code():
    """Update and install main project code only, restart Django

    Doesn't update any dependencies.

    This works for installations where the project code is installed into the
    virtualenv.  This is done with::

        pip install -U -e <repository>

    """
    pip.update_repo('git+'
                    'ssh://{repository}'
                    '@{branch}'
                    '#egg={project_name}\n'.format(**env))
    restart_django()


@task
def update_code_checkout():
    """Update main project code only, restart Django

    Doesn't update any dependencies.

    This works for direct checkouts from a project repository when the code is
    *not* installed into the virtualenv.  The update is done with::

        git pull

    """
    with virtualenv():
        run('git pull')
    restart_django()


@task
def update():
    update_python_packages()
    restart_django()


@task
def syncdb():
    raise DeprecationWarning("Call manage('syncdb') or "
                             "type manage:syncdb on the command line instead")
    manage('syncdb')


@task
def migrate():
    raise DeprecationWarning("Call manage('migrate') or "
                             "type manage:migrate on the command line instead")
    manage('migrate')
