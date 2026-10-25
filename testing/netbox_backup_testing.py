###################################################################
#  Test configuration for the netbox-backup plugin (derived from  #
#  NetBox's configuration_testing.py). Not for production use.    #
###################################################################

import os

ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': {
        'NAME': os.environ.get('NETBOX_DB_NAME', 'netbox'),
        # CI runs as a non-superuser role that owns its databases, as on a shared PaaS server
        'USER': os.environ.get('NETBOX_DB_USER', 'netbox'),
        'PASSWORD': os.environ.get('NETBOX_DB_PASSWORD', 'netbox'),
        'HOST': 'localhost',
        'PORT': '',
        'CONN_MAX_AGE': 300,
    }
}

PLUGINS = [
    'netbox_backup',
]

# Tests configure targets, policies and generated keys per test case
PLUGINS_CONFIG = {
    'netbox_backup': {
        'instance_id': '0b1d3c5e-7f90-4a1b-8c2d-3e4f5a6b7c8d',
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
