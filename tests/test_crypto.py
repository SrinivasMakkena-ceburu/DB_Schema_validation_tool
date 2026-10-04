import pytest

from validator import crypto
from validator.models import DatabaseTarget, Project


def test_encrypt_roundtrip():
    token = crypto.encrypt("p@ss:wörd")
    assert token != "p@ss:wörd"
    assert crypto.decrypt(token) == "p@ss:wörd"


@pytest.mark.django_db
def test_database_password_roundtrip():
    db = DatabaseTarget(name="stage", host="h", dbname="d", user="u")
    db.set_password("s3cret")
    db.save()
    db.refresh_from_db()
    assert "s3cret" not in db.password_encrypted
    assert db.get_password() == "s3cret"


@pytest.mark.django_db
def test_database_blank_password_means_pgpass():
    db = DatabaseTarget(name="stage", host="h", dbname="d", user="u")
    db.set_password("")
    assert db.password_encrypted == ""
    assert "password" not in db.conninfo()


def test_project_env_dict():
    project = Project(extra_env="A=1\n# comment\n\nB = two\nC=x=y\n")
    assert project.env_dict() == {"A": "1", "B": "two", "C": "x=y"}
