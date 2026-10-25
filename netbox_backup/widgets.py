from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _
from extras.dashboard.utils import register_widget
from extras.dashboard.widgets import DashboardWidget


@register_widget
class BackupHealthWidget(DashboardWidget):
    """
    Green only if every enabled policy succeeded within its interval + max_runtime + 1 h and at least one locked
    off-site copy exists.
    """
    default_title = _('Backup health')
    description = _('Whether this NetBox can be recovered from its backups right now.')
    width = 4
    height = 3

    def render(self, request):
        from .engine import health
        if not request.user.has_perm('netbox_backup.view_backupset'):
            return render_to_string('netbox_backup/widget.html', {'denied': True})
        try:
            conds = health.conditions()
        except Exception as e:
            return render_to_string('netbox_backup/widget.html', {'error': str(e)})
        overall = health.overall(conds)
        return render_to_string('netbox_backup/widget.html', {
            'overall': overall,
            'color': health.COLORS[overall],
            'problems': [c for c in conds if c.status in (health.WARNING, health.ERROR)][:6],
        })
