from django.db import models

# Create your models here.

class Producto(models.Model):
    # Campos básicos
    nombre = models.CharField(max_length=255)
    
    # ... otros campos (código de barras, categoría, etc.) ...

    # Nivel 1 de Volumen
    volumen_nombre = models.CharField(max_length=100, blank=True, null=True)
    volumen_cantidad = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    precio_volumen_oferta = models.DecimalField(max_digits=10, decimal_places=4, default=0)

    # Nivel 2 de Volumen
    volumen_nombre_2 = models.CharField(max_length=100, blank=True, null=True)
    volumen_cantidad_2 = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    volumen_precio_2 = models.DecimalField(max_digits=10, decimal_places=4, default=0)

    # Nivel 3 de Volumen
    volumen_nombre_3 = models.CharField(max_length=100, blank=True, null=True)
    volumen_cantidad_3 = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    volumen_precio_3 = models.DecimalField(max_digits=10, decimal_places=4, default=0)

    def __str__(self):
        return self.nombre