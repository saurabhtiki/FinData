from django.urls import path

from . import api_views, browse_views, db_views, upload_views, views

app_name = "findata"

urlpatterns = [
    path("", views.datasets, name="datasets"),
    path("new/", views.new_dataset, name="new"),
    path("new/blank/", views.new_blank, name="new_blank"),
    path("new/sample/", views.new_from_sample, name="new_sample"),
    path("new/copy/<int:pk>/", views.new_from_copy, name="new_copy"),
    path("new/save/", views.create, name="create"),
    path("<int:pk>/", browse_views.data, name="data"),
    path("<int:pk>/changes/", browse_views.changes, name="changes"),
    path("<int:pk>/row-history/", browse_views.row_history, name="row_history"),
    path("uploads/", browse_views.recent_uploads, name="recent_uploads"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/delete/", views.delete, name="delete"),
    path("<int:pk>/template/", views.template, name="template"),
    path("<int:pk>/upload/", upload_views.start, name="upload"),
    path("<int:pk>/history/", upload_views.history, name="history"),
    path("<int:pk>/dates/", upload_views.dates, name="dates"),
    path("<int:pk>/dates/how-often/", upload_views.how_often, name="dates_how_often"),
    path("<int:pk>/dates/<str:day>/remove/", upload_views.remove_date, name="date_remove"),
    path("<int:pk>/upload/database/", db_views.pull, name="upload_pull"),
    path("<int:pk>/upload/database/tables/", db_views.server_tables, name="upload_tables"),
    path("<int:pk>/upload/api/", api_views.api_fetch, name="upload_api"),
    path("<int:pk>/upload/api/test/", api_views.api_test, name="upload_api_test"),
    path("uploads/<int:upload_id>/table/", db_views.sqlite_pick, name="upload_sqlite"),
    path("connections/", db_views.connections, name="connections"),
    path("connections/new/", db_views.connection_edit, name="connection_new"),
    path("connections/test/", db_views.connection_test, name="connection_test"),
    path("connections/<int:connection_id>/", db_views.connection_edit, name="connection_edit"),
    path(
        "connections/<int:connection_id>/delete/",
        db_views.connection_delete,
        name="connection_delete",
    ),
    path("uploads/<int:upload_id>/map/", upload_views.map_columns, name="upload_map"),
    path("uploads/<int:upload_id>/problems/", upload_views.problems, name="upload_problems"),
    path(
        "uploads/<int:upload_id>/problems.xlsx",
        upload_views.problems_file,
        name="upload_problems_file",
    ),
    path("uploads/<int:upload_id>/", upload_views.preview, name="upload_preview"),
    path("uploads/<int:upload_id>/confirm/", upload_views.confirm, name="upload_confirm"),
    path("uploads/<int:upload_id>/cancel/", upload_views.cancel, name="upload_cancel"),
    path("uploads/<int:upload_id>/undo/", upload_views.undo, name="upload_undo"),
]
