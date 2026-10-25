from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_backup-api'

router = NetBoxRouter()
router.register('targets', views.StorageTargetViewSet)
router.register('policies', views.BackupPolicyViewSet)
router.register('sets', views.BackupSetViewSet)
router.register('restores', views.RestoreRunViewSet)

urlpatterns = [
    path('status/', views.StatusView.as_view(), name='status'),
] + router.urls
