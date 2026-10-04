from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)


class Tag(models.Model):
    label = models.CharField(max_length=50)


class Product(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    created = models.DateTimeField(db_index=True)
    tags = models.ManyToManyField(Tag, blank=True)
    sku = models.CharField(max_length=40, default="")

    class Meta:
        unique_together = [("category", "name")]
