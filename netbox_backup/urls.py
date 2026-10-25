from django.urls import include, path
from utilities.urls import get_model_urls

from . import views  # noqa: F401  (registers model views)

urlpatterns = [
    path('', views.StatusView.as_view(), name='status'),
    path('targets/', include(get_model_urls('netbox_backup', 'storagetarget', detail=False))),
    path('targets/<int:pk>/', include(get_model_urls('netbox_backup', 'storagetarget'))),
    path('policies/', include(get_model_urls('netbox_backup', 'backuppolicy', detail=False))),
    path('policies/<int:pk>/', include(get_model_urls('netbox_backup', 'backuppolicy'))),
    path('sets/', include(get_model_urls('netbox_backup', 'backupset', detail=False))),
    path('sets/<int:pk>/', include(get_model_urls('netbox_backup', 'backupset'))),
    path('restores/', include(get_model_urls('netbox_backup', 'restorerun', detail=False))),
    path('restores/<int:pk>/', include(get_model_urls('netbox_backup', 'restorerun'))),
]
