"""
The secrets inside a set's config component: SECRET_KEY, API_TOKEN_PEPPERS and the database password, read from
configuration.py without executing it. Values assigned from the environment or computed in code are reported as
unknown rather than guessed.
"""
import ast
import json

UNKNOWN = object()


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return UNKNOWN


def from_configuration(source):
    """
    {'SECRET_KEY': str|UNKNOWN, 'API_TOKEN_PEPPERS': {id: str}|UNKNOWN, 'DB_PASSWORD': str|UNKNOWN} for the names
    configuration.py assigns at module level.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name, value = node.targets[0].id, _literal(node.value)
        if name in ('SECRET_KEY', 'API_TOKEN_PEPPERS'):
            found[name] = value
        elif name in ('DATABASE', 'DATABASES'):
            db = value.get('default', value) if isinstance(value, dict) else UNKNOWN
            found['DB_PASSWORD'] = db.get('PASSWORD', UNKNOWN) if isinstance(db, dict) else UNKNOWN
    return found


def from_config_component(document):
    """
    Secrets from the decrypted config.json: the first configuration file that assigns any.
    """
    for path, source in (document.get('files') or {}).items():
        if path.endswith('.py') and 'ldap' not in path.rsplit('/', 1)[-1]:
            found = from_configuration(source)
            if found:
                return found
    return {}


def env_lines(secrets):
    """
    The secrets as KEY='value' lines in netbox-docker's naming (SECRET_KEY, API_TOKEN_PEPPER_<id>, DB_PASSWORD).
    """
    lines = []

    def emit(key, value):
        if value is UNKNOWN or value is None:
            lines.append(f"# {key}: not a literal in configuration.py (set from the environment or code)")
        else:
            lines.append(f"{key}={json.dumps(str(value))}")

    if 'SECRET_KEY' in secrets:
        emit('SECRET_KEY', secrets['SECRET_KEY'])
    peppers = secrets.get('API_TOKEN_PEPPERS')
    if isinstance(peppers, dict):
        for pepper_id, value in sorted(peppers.items()):
            emit(f'API_TOKEN_PEPPER_{pepper_id}', value)
    elif 'API_TOKEN_PEPPERS' in secrets:
        emit('API_TOKEN_PEPPERS', peppers)
    if 'DB_PASSWORD' in secrets:
        emit('DB_PASSWORD', secrets['DB_PASSWORD'])
    return lines


def differences(from_set, current):
    """
    Names of secrets that differ between the set and this host (values are never returned), and names that can't
    be compared because the set's value isn't a literal.
    """
    differ, unknown = [], []
    for name, value in from_set.items():
        if value is UNKNOWN:
            unknown.append(name)
            continue
        here = current.get(name)
        if name == 'API_TOKEN_PEPPERS' and isinstance(value, dict) and isinstance(here, dict):
            differ += [f'API_TOKEN_PEPPERS[{k}]' for k in sorted(set(value) | set(here), key=str)
                       if value.get(k) != here.get(k)]
        elif value != here:
            differ.append(name)
    return differ, unknown
