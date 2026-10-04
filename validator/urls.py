from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("batches/<uuid:batch_id>/", views.batch_detail, name="batch_detail"),
    path("runs/", views.run_list, name="run_list"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/fix.sql", views.run_fix_sql, name="run_fix_sql"),
    path("runs/<int:pk>/report.json", views.run_report_json, name="run_report_json"),
    path("projects/", views.project_list, name="project_list"),
    path("projects/new/", views.project_form, name="project_create"),
    path("projects/<int:pk>/edit/", views.project_form, name="project_edit"),
    path("projects/<int:pk>/check/", views.project_check, name="project_check"),
    path("projects/<int:pk>/delete/", views.delete_object, {"kind": "project"}, name="project_delete"),
    path("databases/", views.database_list, name="database_list"),
    path("databases/new/", views.database_form, name="database_create"),
    path("databases/<int:pk>/edit/", views.database_form, name="database_edit"),
    path("databases/<int:pk>/test/", views.database_test, name="database_test"),
    path("databases/<int:pk>/delete/", views.delete_object, {"kind": "database"}, name="database_delete"),
]
