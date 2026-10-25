from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_backup-api'

router = NetBoxRouter()
router.register('backups', views.BackupViewSet)
router.register('restores', views.RestoreEventViewSet)

urlpatterns = [
    path('status/', views.StatusView.as_view(), name='status'),
    path('backups/run/', views.RunBackupView.as_view(), name='backup-run'),
    path('backups/sync/', views.SyncCatalogView.as_view(), name='backup-sync'),
    path('backups/<int:pk>/verify/', views.VerifyBackupView.as_view(), name='backup-verify'),
] + router.urls
