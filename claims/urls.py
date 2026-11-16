from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api, views

app_name = "claims"

router = DefaultRouter()
router.register("claims", api.ClaimViewSet, basename="api-claim")

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("claims/new/", views.claim_create, name="create"),
    path("claims/<int:pk>/", views.claim_detail, name="detail"),
    path("claims/<int:pk>/message/", views.claim_add_message, name="message"),
    path("claims/<int:pk>/documents/", views.claim_add_documents, name="add_documents"),
    path("claims/<int:pk>/status/", views.claim_update_status, name="update_status"),
    path("documents/<int:pk>/", views.document_download, name="document"),
    path("panel/", views.panel_home, name="panel_home"),
    path("panel/users/", views.panel_users, name="panel_users"),
    path("panel/users/<int:pk>/", views.panel_user_edit, name="panel_user_edit"),
    path("api/", include(router.urls)),
]
