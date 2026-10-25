from django.urls import include, path
from utilities.urls import get_model_urls

from . import views  # noqa: F401  (registers model views)

urlpatterns = [
    path('', views.BackupStatusView.as_view(), name='status'),
    path('backups/', include(get_model_urls('netbox_backup', 'backup', detail=False))),
    path('backups/<int:pk>/', include(get_model_urls('netbox_backup', 'backup'))),
    path('restores/', include(get_model_urls('netbox_backup', 'restoreevent', detail=False))),
    path('restores/<int:pk>/', include(get_model_urls('netbox_backup', 'restoreevent'))),
]
