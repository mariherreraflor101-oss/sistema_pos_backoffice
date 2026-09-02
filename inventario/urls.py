from django.urls import path
from . import views

urlpatterns = [
    # Tu ruta actual para el panel web
    path('compras/', views.gestionar_compras, name='gestionar_compras'),
    path('login/', views.login_pos, name='login_pos'),
    
    # ==========================================
    # NUEVAS RUTAS PARA LA APLICACIÓN FLUTTER
    # ==========================================
    path('api/historial/', views.api_historial_compras, name='api_historial'),
    path('api/productos/', views.api_buscar_productos, name='api_productos'),
    path('api/compras/registrar/', views.api_registrar_compra_app, name='api_registrar_compra_app'),
]