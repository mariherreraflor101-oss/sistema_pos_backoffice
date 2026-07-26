from django.contrib import admin
from django.urls import path, include  # <-- Agrega "include" aquí

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('inventario.urls')), # <-- Conecta tu app de inventario
]