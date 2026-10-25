from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label=_('Backup'),
    icon_class='mdi mdi-backup-restore',
    groups=(
        (_('Backup'), (
            PluginMenuItem(link='plugins:netbox_backup:storagetarget_list', link_text=_('Targets'),
                           permissions=['netbox_backup.view_storagetarget']),
            PluginMenuItem(link='plugins:netbox_backup:backuppolicy_list', link_text=_('Policies'),
                           permissions=['netbox_backup.view_backuppolicy']),
            PluginMenuItem(link='plugins:netbox_backup:backupset_list', link_text=_('Backup sets'),
                           permissions=['netbox_backup.view_backupset']),
            PluginMenuItem(link='plugins:netbox_backup:restorerun_list', link_text=_('Restores'),
                           permissions=['netbox_backup.view_restorerun']),
            PluginMenuItem(link='plugins:netbox_backup:status', link_text=_('Status'),
                           permissions=['netbox_backup.view_backupset']),
        )),
    ),
)
