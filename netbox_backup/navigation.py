from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label=_('Backup & Recovery'),
    icon_class='mdi mdi-backup-restore',
    groups=(
        (_('Backup & Recovery'), (
            PluginMenuItem(
                link='plugins:netbox_backup:status',
                link_text=_('Status'),
                permissions=['netbox_backup.view_backup'],
            ),
            PluginMenuItem(
                link='plugins:netbox_backup:backup_list',
                link_text=_('Backups'),
                permissions=['netbox_backup.view_backup'],
                buttons=(
                    PluginMenuButton(
                        link='plugins:netbox_backup:backup_run',
                        title=_('Run backup'),
                        icon_class='mdi mdi-play',
                        permissions=['netbox_backup.run_backup'],
                    ),
                ),
            ),
            PluginMenuItem(
                link='plugins:netbox_backup:restoreevent_list',
                link_text=_('Restores'),
                permissions=['netbox_backup.view_restoreevent'],
            ),
        )),
    ),
)
