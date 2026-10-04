from django.db import models


class Order(models.Model):
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    created = models.DateTimeField()
    # Added without a migration: the extractor must report it as pending.
    note = models.TextField(blank=True, default="")
