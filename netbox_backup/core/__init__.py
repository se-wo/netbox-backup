"""
The Django-free core of netbox-backup: configuration, storage drivers, GnuPG, the set format and the streaming
pipeline. Both the NetBox plugin and the standalone ``netbox-backup`` CLI are built on it, so backups can be listed,
verified and decrypted when NetBox itself cannot start.
"""
