"""Every kind of relation the cascade planner has to handle."""
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=100)
    notes = GenericRelation("devices.Note")


class PremiumCustomer(Customer):  # multi-table inheritance
    tier = models.CharField(max_length=20, default="gold")


class Operator(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    email = models.CharField(max_length=200)
    password = models.CharField(max_length=128, default="")


class Device(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE)
    hostname = models.CharField(max_length=100)


class Execution(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)  # second path: diamond
    operator = models.ForeignKey(Operator, null=True, blank=True, on_delete=models.SET_NULL)
    started = models.DateTimeField()
    status = models.CharField(max_length=20)


class Contract(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    code = models.CharField(max_length=20)


class AuditEntry(models.Model):
    device = models.ForeignKey(Device, on_delete=models.RESTRICT)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)


class Note(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    text = models.TextField()


class Group(models.Model):
    name = models.CharField(max_length=50)
    devices = models.ManyToManyField(Device, blank=True)
