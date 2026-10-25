###################################################################
#  Test configuration for the netbox-backup plugin (derived from  #
#  NetBox's configuration_testing.py)                             #
#  only. It is not intended for production use.                   #
###################################################################

ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': {
        'NAME': 'netbox',
        'USER': 'netbox',
        'PASSWORD': 'netbox',
        'HOST': 'localhost',
        'PORT': '',
        'CONN_MAX_AGE': 300,
    }
}

PLUGINS = [
    'netbox_backup',
]

PLUGINS_CONFIG = {
    'netbox_backup': {
        'storage': {'backend': 'local', 'path': '/tmp/netbox-backup-tests/storage'},
        'work_dir': '/tmp/netbox-backup-tests/work',
    },
}

RQ = {
    'COMMIT_MODE': 'auto',
}

REDIS = {
    'tasks': {
        'HOST': 'localhost',
        'PORT': 6379,
        'USERNAME': '',
        'PASSWORD': '',
        'DATABASE': 0,
        'SSL': False,
    },
    'caching': {
        'HOST': 'localhost',
        'PORT': 6379,
        'USERNAME': '',
        'PASSWORD': '',
        'DATABASE': 1,
        'SSL': False,
    }
}

SECRET_KEY = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

DEFAULT_PERMISSIONS = {}

API_TOKEN_PEPPERS = {
    1: 'TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE',
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': True
}
