import json
import requests  # 🚀 NUEVO: Librería para comunicarnos con el Subdominio
import firebase_admin
from firebase_admin import credentials, firestore
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from datetime import datetime, timedelta, timezone 
from django.views.decorators.csrf import csrf_exempt

# ==========================================================
# 🔴 ENLACE AL MAESTRO (Cambia esto por el dominio real de tu Subdominio)
# ==========================================================
URL_MAESTRO = "https://api-santarosa.bodegaelpueblo.com"  # O la URL que uses para tu panel de la App

# Inicializar Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-credentials.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

def to_float(val, default=0.0):
    if val is None: return default
    val_str = str(val).strip().replace(',', '.')
    if not val_str: return default
    try: return float(val_str)
    except ValueError: return default

def obtener_valor_flexible(data, claves, default=0.0):
    for clave in claves:
        val = data.get(clave)
        if val is not None:
            try: return float(val)
            except (ValueError, TypeError): pass
    return default

def gestionar_compras(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.POST.get('ajax') == 'true'

        # ========================================================
        # 🚀 NUEVO: PUENTES DE CONEXIÓN PARA LEER DEL SUBDOMINIO
        # ========================================================
        if action == 'obtener_categorias':
            try:
                res = requests.get(f"{URL_MAESTRO}/api/interno/categorias/", timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                print("Error obteniendo categorías:", e)
                return JsonResponse({'status': 'error', 'categorias': []})

        elif action == 'obtener_subcategorias':
            cat_id = request.POST.get('categoria_id')
            try:
                res = requests.get(f"{URL_MAESTRO}/api/interno/subcategorias/{cat_id}/", timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                print("Error obteniendo subcategorías:", e)
                return JsonResponse({'status': 'error', 'subcategorias': []})

        # ========================================================
        # 🚀 LA DOBLE ACCIÓN: CREAR PRODUCTO
        # ========================================================
        elif action == 'crear_producto':
            try:
                nombre = request.POST.get('nuevo_nombre', '').strip().upper()
                codigo_barras = request.POST.get('nuevo_codigo_barras', '').strip()
                es_granel = request.POST.get('nuevo_es_granel') == 'on'
                
                categoria_id = request.POST.get('nueva_categoria_id') 
                subcategoria_id = request.POST.get('nueva_subcategoria_id') # 🔴 NUEVO
                
                precio_menor = to_float(request.POST.get('nuevo_precio_menor'))
                precio_mayor = to_float(request.POST.get('nuevo_precio_mayor'))

                # --- 1. ENVIAR AL SUBDOMINIO (MYSQL + CLOUDFLARE) ---
                archivos = {}
                if 'nueva_imagen' in request.FILES:
                    img = request.FILES['nueva_imagen']
                    archivos = {'imagen': (img.name, img.read(), img.content_type)}
                
                # 🔴 CORRECCIÓN: Paquete limpio solo con lo que la App necesita
                datos_mysql = {
                    'nombre': nombre,
                    'precio_final': precio_menor,
                    'categoria_id': categoria_id,
                    'subcategoria_id': subcategoria_id,
                }
                
                try:
                    # Disparamos la petición al cerebro maestro
                    requests.post(f"{URL_MAESTRO}/api/interno/recibir_producto/", data=datos_mysql, files=archivos, timeout=5)
                except Exception as api_err:
                    print("⚠️ Advertencia:", api_err)

                # --- 2. GUARDAR EN FIREBASE (PARA LAS CAJERAS) ---
                nuevo_producto = {
                    'nombre': nombre,
                    'tiene_imagen': 'nueva_imagen' in request.FILES, # 🚀 NUEVO: Deja la huella
                    'codigo_barras': codigo_barras,
                    'venta_granel': es_granel,
                    'precio': precio_menor,             
                    'volumen_precio': precio_mayor,
                    # ... (resto del código intacto)
                    'categoria_id': categoria_id,
                    'subcategoria_id': subcategoria_id,
                    'ventas_mes': 0,           
                    'stock_infinito': True,    
                    'fecha_creacion': firestore.SERVER_TIMESTAMP
                }
                
                time_fb, doc_ref = db.collection('productos').add(nuevo_producto)
                
                detalle_auditoria = 'Registrado desde panel Web (Sincronizado a Maestro y Firebase)'
                if precio_menor > 0 or precio_mayor > 0: 
                    detalle_auditoria += f' (Público: S/{precio_menor} | Mayor: S/{precio_mayor})'

                db.collection('auditoria_productos').add({
                    'tipo': 'NUEVO_PRODUCTO',
                    'producto_nombre': nombre,
                    'detalle': detalle_auditoria,
                    'usuario': 'Admin Web',
                    'fecha': firestore.SERVER_TIMESTAMP,
                })
                
                if is_ajax:
                    return JsonResponse({
                        'status': 'success', 
                        'message': f'🟩 ¡Producto "{nombre}" creado y sincronizado con éxito!',
                        'nuevo_producto': {
                            'id': doc_ref.id,
                            'nombre': nombre,
                            'precio_actual': precio_menor,
                            'precio_mayor': precio_mayor,
                            'paquete_nombre': '',
                            'paquete_codigo': '',
                            'paquete_cantidad': 1,
                            'volumen_nombre': '',
                            'volumen_cantidad': '',
                            'volumen_precio_oferta': '',
                            'venta_granel': es_granel 
                        }
                    })
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error al crear: {str(e)}'})

        # ... EL RESTO DE TUS MÉTODOS ORIGINALES ...
        elif action == 'actualizar_nombre':
            try:
                producto_id = request.POST.get('producto_id')
                nuevo_nombre = request.POST.get('nuevo_nombre', '').strip().upper()
                if producto_id and nuevo_nombre:
                    db.collection('productos').document(producto_id).update({'nombre': nuevo_nombre})
                    if is_ajax: return JsonResponse({'status': 'success', 'message': f'✅ Nombre actualizado a "{nuevo_nombre}"', 'nombre_actualizado': nuevo_nombre})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'registrar_compra':
            try:
                lote_str = request.POST.get('productos_lote')
                if not lote_str: raise ValueError("No hay productos en el lote")
                lote = json.loads(lote_str)
                costo_total_global = to_float(request.POST.get('costo_total'))
                precio_menor = to_float(request.POST.get('precio_menor'))
                precio_mayor = to_float(request.POST.get('precio_mayor'))
                bonos_externos_json = request.POST.get('bonos_externos_json')
                bonos_externos = json.loads(bonos_externos_json) if bonos_externos_json else []
                bonificacion_interna = 0.0
                valor_descuento_externo = 0.0

                if len(bonos_externos) > 0:
                    for b in bonos_externos:
                        valor_descuento_externo += (to_float(b.get('cantidad')) * to_float(b.get('precio')))
                    costo_total_global = max(0.0, costo_total_global - valor_descuento_externo)
                else:
                    bonificacion_interna = to_float(request.POST.get('bonificacion'))

                unidades_base_totales = sum(to_float(p.get('cantidad')) * to_float(p.get('factor'), 1.0) for p in lote)
                unidades_reales_totales = unidades_base_totales + bonificacion_interna
                costo_unitario_real = costo_total_global / unidades_reales_totales if unidades_reales_totales > 0 else 0.0

                for p in lote:
                    p_id = p.get('id')
                    unidades_fila = to_float(p.get('cantidad')) * to_float(p.get('factor'), 1.0)
                    bono_proporcional = (unidades_fila / unidades_base_totales) * bonificacion_interna if unidades_base_totales > 0 else 0.0
                    costo_proporcional = costo_unitario_real * (unidades_fila + bono_proporcional)

                    db.collection('compras_inventario').add({
                        'producto_id': p_id,
                        'producto_nombre': p.get('nombre'),
                        'fecha': firestore.SERVER_TIMESTAMP,
                        'cantidad_paquetes': to_float(p.get('cantidad')),
                        'factor': to_float(p.get('factor'), 1.0),
                        'bonificacion': bono_proporcional, 
                        'costo_total': costo_proporcional,
                        'costo_unitario': costo_unitario_real,
                        'precio_menor_registrado': precio_menor,
                        'precio_mayor_registrado': precio_mayor,
                        'is_menor': False if precio_mayor > 0 else True,
                        'activo': True
                    })

                    db.collection('productos').document(p_id).update({
                        'precio': precio_menor,
                        'volumen_precio': precio_mayor,
                        'ultimo_costo': costo_unitario_real,
                    })

                if len(bonos_externos) > 0:
                    for b in bonos_externos:
                        b_cant = to_float(b.get('cantidad'))
                        if b_cant > 0:
                            b_precio = to_float(b.get('precio'))
                            db.collection('compras_inventario').add({
                                'producto_id': b.get('id'),
                                'producto_nombre': f"🎁 REGALO: {b.get('nombre')}",
                                'fecha': firestore.SERVER_TIMESTAMP,
                                'cantidad_paquetes': b_cant,
                                'factor': 1.0,
                                'bonificacion': 0.0, 
                                'costo_total': (b_cant * b_precio), 
                                'costo_unitario': b_precio,
                                'precio_menor_registrado': b_precio,
                                'precio_mayor_registrado': b_precio,
                                'is_menor': True,
                                'activo': True
                            })

                if is_ajax: return JsonResponse({'status': 'success', 'message': f'✅ Ingreso de Lote guardado con éxito.'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error al registrar lote: {str(e)}'})
                
        elif action == 'guardar_configuracion': 
            try:
                producto_id = request.POST.get('producto_id')
                tipo_configuracion = request.POST.get('tipo_configuracion')
                datos_actualizar = {}
                
                if tipo_configuracion == 'paquete':
                    datos_actualizar['paquete_nombre'] = request.POST.get('paquete_nombre', '').strip()
                    datos_actualizar['paquete_cantidad'] = int(to_float(request.POST.get('factor_paquete'), 1.0))
                    datos_actualizar['paquete_codigo'] = request.POST.get('paquete_codigo', '').strip()
                else:
                    datos_actualizar['volumen_nombre'] = request.POST.get('volumen_nombre', '').strip()
                    datos_actualizar['volumen_cantidad'] = int(to_float(request.POST.get('cantidad_minima'), 0.0))
                    datos_actualizar['volumen_precio'] = to_float(request.POST.get('precio_volumen_oferta'))
                    
                    cant_2 = int(to_float(request.POST.get('cantidad_minima_2'), 0.0))
                    if cant_2 > 0:
                        datos_actualizar['volumen_nombre_2'] = request.POST.get('volumen_nombre_2', '').strip()
                        datos_actualizar['volumen_cantidad_2'] = cant_2
                        datos_actualizar['volumen_precio_2'] = to_float(request.POST.get('precio_volumen_oferta_2'))
                    else:
                        datos_actualizar['volumen_nombre_2'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_cantidad_2'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_precio_2'] = firestore.DELETE_FIELD

                    cant_3 = int(to_float(request.POST.get('cantidad_minima_3'), 0.0))
                    if cant_3 > 0:
                        datos_actualizar['volumen_nombre_3'] = request.POST.get('volumen_nombre_3', '').strip()
                        datos_actualizar['volumen_cantidad_3'] = cant_3
                        datos_actualizar['volumen_precio_3'] = to_float(request.POST.get('precio_volumen_oferta_3'))
                    else:
                        datos_actualizar['volumen_nombre_3'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_cantidad_3'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_precio_3'] = firestore.DELETE_FIELD

                db.collection('productos').document(producto_id).update(datos_actualizar)
                if is_ajax: return JsonResponse({'status': 'success', 'message': 'Configuración guardada'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error: {str(e)}'})

        elif action == 'eliminar_producto':
            try:
                producto_id = request.POST.get('producto_id')
                if producto_id:
                    db.collection('productos').document(producto_id).delete()
                    if is_ajax: return JsonResponse({'status': 'success', 'message': '🗑️ Producto eliminado.'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'filtrar_historial':
            try:
                fecha_desde = datetime.strptime(request.POST.get('fecha_desde'), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                fecha_hasta = (datetime.strptime(request.POST.get('fecha_hasta'), "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)

                compras_ref = db.collection('compras_inventario').where('fecha', '>=', fecha_desde).where('fecha', '<', fecha_hasta).order_by('fecha', direction=firestore.Query.DESCENDING).stream()

                historial_filtrado = []
                for doc in compras_ref:
                    data = doc.to_dict()
                    fecha_obj = data.get('fecha')
                    historial_filtrado.append({
                        'compra_id': doc.id,
                        'producto_id': data.get('producto_id', ''),
                        'producto': data.get('producto_nombre', ''),
                        'cantidad': obtener_valor_flexible(data, ['cantidad_paquetes', 'cantidad_ingresada_paquetes', 'cantidad'], 0),
                        'factor': obtener_valor_flexible(data, ['factor', 'factor_unidades', 'peso'], 1),
                        'bonificacion': obtener_valor_flexible(data, ['bonificacion', 'bono'], 0),
                        'total_pagado': obtener_valor_flexible(data, ['costo_total'], 0),
                        'precio_mayor': obtener_valor_flexible(data, ['precio_mayor_registrado', 'precio_mayor'], 0),
                        'precio_menor': obtener_valor_flexible(data, ['precio_menor_registrado', 'precio_menor'], 0),
                        'is_menor': data.get('is_menor', True), 
                        'fecha': fecha_obj.strftime("%d/%m/%Y") if fecha_obj else "Sin fecha",
                        'activo': data.get('activo', True)
                    })
                if is_ajax: return JsonResponse({'status': 'success', 'historial': historial_filtrado})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'actualizar_proyeccion':
            try:
                compra_id = request.POST.get('compra_id')
                if compra_id:
                    db.collection('compras_inventario').document(compra_id).update({'is_menor': request.POST.get('is_menor') == 'true'})
                    if is_ajax: return JsonResponse({'status': 'success'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'actualizar_precios_historial':
            try:
                compra_id = request.POST.get('compra_id')
                producto_id = request.POST.get('producto_id')
                
                p_mayor = to_float(request.POST.get('precio_mayor'))
                p_menor = to_float(request.POST.get('precio_menor'))
                cantidad = to_float(request.POST.get('cantidad'))
                factor = to_float(request.POST.get('factor'), 1.0)
                costo_total = to_float(request.POST.get('costo_total'))
                es_granel = request.POST.get('es_granel') == 'true'

                costo_unitario = costo_total / ((cantidad * factor) + to_float(request.POST.get('bono'))) if ((cantidad * factor) + to_float(request.POST.get('bono'))) > 0 else 0.0

                if compra_id:
                    db.collection('compras_inventario').document(compra_id).update({
                        'cantidad_paquetes': cantidad, 'factor': factor, 'costo_total': costo_total,
                        'costo_unitario': costo_unitario, 'precio_mayor_registrado': p_mayor, 'precio_menor_registrado': p_menor
                    })
                if producto_id:
                    db.collection('productos').document(producto_id).update({
                        'venta_granel': es_granel, 'volumen_precio': p_mayor, 'precio': p_menor, 'ultimo_costo': costo_unitario
                    })

                if is_ajax: return JsonResponse({'status': 'success', 'message': f'✅ Compra corregida.'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'eliminar_compra':
            try:
                compra_id = request.POST.get('compra_id')
                if compra_id:
                    db.collection('compras_inventario').document(compra_id).delete()
                    if is_ajax: return JsonResponse({'status': 'success', 'message': f'🗑️ Compra eliminada permanentemente.'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})        

        elif action == 'editar_producto_catalogo':
            try:
                producto_id = request.POST.get('producto_id')
                nombre = request.POST.get('editar_nombre', '').strip().upper()
                codigo_barras = request.POST.get('editar_codigo_barras', '').strip()
                es_granel = request.POST.get('editar_es_granel') == 'on'
                categoria_id = request.POST.get('editar_categoria_id')
                subcategoria_id = request.POST.get('editar_subcategoria_id') # 🔴 NUEVO
                precio_menor = to_float(request.POST.get('editar_precio_menor'))
                precio_mayor = to_float(request.POST.get('editar_precio_mayor'))

                # ==========================================================
                # 🚀 REGLA DE ORO: Enviar al Subdominio SOLO SI trae imagen
                # ==========================================================
                if 'editar_imagen' in request.FILES:
                    img = request.FILES['editar_imagen']
                    archivos = {'imagen': (img.name, img.read(), img.content_type)}
                    
                    datos_mysql = {
                        'nombre': nombre,
                        'precio_final': precio_menor,
                        'categoria_id': categoria_id,
                        'subcategoria_id': subcategoria_id,
                    }
                    try:
                        # Lo enviamos al Subdominio para que lo procese (Update or Create)
                        requests.post(f"{URL_MAESTRO}/api/interno/recibir_producto/", data=datos_mysql, files=archivos, timeout=5)
                    except Exception as api_err:
                        print("⚠️ Advertencia: Error conectando con el Subdominio:", api_err)

                # ==========================================================
                # 💾 SIEMPRE ACTUALIZAR EN FIREBASE (POS local)
                # ==========================================================
                if producto_id:
                    datos_actualizar = {
                        'nombre': nombre,
                        'codigo_barras': codigo_barras,
                        'venta_granel': es_granel,
                        'precio': precio_menor,             
                        'volumen_precio': precio_mayor,
                    }
                    if 'editar_imagen' in request.FILES: # 🚀 NUEVO: Deja la huella si subió foto
                        datos_actualizar['tiene_imagen'] = True
                    if categoria_id:
                        datos_actualizar['categoria_id'] = categoria_id
                    if subcategoria_id:
                        datos_actualizar['subcategoria_id'] = subcategoria_id

                    db.collection('productos').document(producto_id).update(datos_actualizar)
                    
                    if is_ajax: 
                        return JsonResponse({'status': 'success', 'message': f'✅ Producto "{nombre}" actualizado.', 'datos': datos_actualizar})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error al editar: {str(e)}'})

        if not is_ajax: return redirect('gestionar_compras')

    # --- RENDERIZADO INICIAL ---
    productos_ref = db.collection('productos').stream()
    lista_productos = []
    for doc in productos_ref:
        data = doc.to_dict()
        lista_productos.append({
            'id': doc.id,
            'nombre': data.get('nombre', 'Sin nombre'),
            'precio_actual': data.get('precio', 0.0),
            'precio_mayor': data.get('volumen_precio', 0.0), 
            'paquete_nombre': data.get('paquete_nombre', ''),
            'paquete_codigo': data.get('paquete_codigo', ''),
            'paquete_cantidad': data.get('paquete_cantidad', 1),
            'codigo_barras': data.get('codigo_barras', ''),
            'categoria_id': data.get('categoria_id', ''),
            'subcategoria_id': data.get('subcategoria_id', ''),
            'volumen_nombre': data.get('volumen_nombre', ''),
            'volumen_cantidad': data.get('volumen_cantidad', ''),
            'volumen_precio_oferta': data.get('volumen_precio', ''),
            'volumen_nombre_2': data.get('volumen_nombre_2', ''),
            'volumen_cantidad_2': data.get('volumen_cantidad_2', ''),
            'volumen_precio_2': data.get('volumen_precio_2', ''),
            'volumen_nombre_3': data.get('volumen_nombre_3', ''),
            'volumen_cantidad_3': data.get('volumen_cantidad_3', ''),
            'volumen_precio_3': data.get('volumen_precio_3', ''),
            'venta_granel': data.get('venta_granel', False),
            'imagen': data.get('tiene_imagen', False) # 🚀 ESTA ES LA LÍNEA QUE MANTIENE EL BRILLO
        })
    productos_json = json.dumps(lista_productos)

    compras_ref = db.collection('compras_inventario').order_by('fecha', direction=firestore.Query.DESCENDING).limit(15).stream()
    historial = []
    for doc in compras_ref:
        data = doc.to_dict()
        fecha_obj = data.get('fecha')
        historial.append({
            'compra_id': doc.id,
            'producto_id': data.get('producto_id', ''),
            'producto': data.get('producto_nombre', ''),
            'cantidad': obtener_valor_flexible(data, ['cantidad_paquetes', 'cantidad_ingresada_paquetes', 'cantidad'], 0),
            'factor': obtener_valor_flexible(data, ['factor', 'factor_unidades', 'peso'], 1),
            'bonificacion': obtener_valor_flexible(data, ['bonificacion', 'bono'], 0),
            'total_pagado': obtener_valor_flexible(data, ['costo_total'], 0),
            'precio_mayor': obtener_valor_flexible(data, ['precio_mayor_registrado', 'precio_mayor'], 0),
            'precio_menor': obtener_valor_flexible(data, ['precio_menor_registrado', 'precio_menor'], 0),
            'is_menor': data.get('is_menor', True), 
            'fecha': fecha_obj.strftime("%d/%m/%Y") if fecha_obj else "Sin fecha"
        })

    return render(request, 'compras.html', {'productos_json': productos_json, 'historial': historial})

# ... EL RESTO DE TUS APIs (api_historial_compras, api_buscar_productos, etc.) QUEDAN INTACTAS ...
# ==========================================================
# APIS EXCLUSIVAS PARA LA APLICACIÓN FLUTTER
# ==========================================================

def api_historial_compras(request):
    """ API 1: Envía el historial de compras a la App Flutter """
    compras_ref = db.collection('compras_inventario').order_by('fecha', direction=firestore.Query.DESCENDING).limit(3000).stream()
    lista = []
    
    for doc in compras_ref:
        data = doc.to_dict()
        fecha_obj = data.get('fecha')
        fecha_str = fecha_obj.strftime("%d/%m/%Y") if fecha_obj else "Sin fecha"
        
        lista.append({
            'id': doc.id,
            'producto_nombre': data.get('producto_nombre', 'Desconocido'),
            'fecha': fecha_str,
            # APLICANDO EL TRADUCTOR A LA API DE FLUTTER
            'costo_total': obtener_valor_flexible(data, ['costo_total'], 0.0),
            'cantidad': obtener_valor_flexible(data, ['cantidad_paquetes', 'cantidad_ingresada_paquetes', 'cantidad'], 0.0),
            'factor': obtener_valor_flexible(data, ['factor', 'factor_unidades', 'peso'], 1.0),
            'bono': obtener_valor_flexible(data, ['bonificacion', 'bono'], 0.0),
            'precio_menor': obtener_valor_flexible(data, ['precio_menor_registrado', 'precio_menor'], 0.0),
            'precio_mayor': obtener_valor_flexible(data, ['precio_mayor_registrado', 'precio_mayor'], 0.0),
            'activo': data.get('activo', True) # <--- AÑADIR ESTA LÍNEA
        })
    return JsonResponse({'compras': lista})

def api_buscar_productos(request):
    """ API 2: Permite a la App Flutter buscar productos y recuperar sus precios base """
    search_query = request.GET.get('search', '').lower()
    productos_ref = db.collection('productos').stream()
    lista = []
    
    for doc in productos_ref:
        data = doc.to_dict()
        nombre = data.get('nombre', '')
        
        if search_query in nombre.lower():
            lista.append({
                'id': doc.id,
                'nombre': nombre,
                'venta_granel': data.get('venta_granel', False),
                'ultimo_costo': data.get('ultimo_costo', 0.0),
                'precio_menor': data.get('precio', 0.0),
                'precio_mayor': data.get('volumen_precio', 0.0),
                'cantidad_mayor': data.get('volumen_cantidad', 3)
            })
            
    return JsonResponse({'productos': lista})

@csrf_exempt
def api_registrar_compra_app(request):
    """ API 3: Recibe una nueva compra desde la App Flutter y la guarda en Firebase """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            producto_id = data.get('producto_id')
            
            cantidad = to_float(data.get('cantidad'))
            factor = to_float(data.get('factor'), 1.0)
            costo_total = to_float(data.get('costo_total'))
            precio_menor = to_float(data.get('precio_menor'))
            precio_mayor = to_float(data.get('precio_mayor'))
            cantidad_mayor = to_float(data.get('cantidad_mayor'))

            unidades_totales = cantidad * factor
            costo_unitario = costo_total / unidades_totales if unidades_totales > 0 else 0.0

            db.collection('compras_inventario').add({
                'producto_id': producto_id,
                'producto_nombre': data.get('producto_nombre', 'Producto desde App'),
                'fecha': firestore.SERVER_TIMESTAMP,
                'cantidad_paquetes': cantidad,
                'factor': factor,
                'bonificacion': 0.0, 
                'costo_total': costo_total,
                'costo_unitario': costo_unitario,
                'precio_menor_registrado': precio_menor,
                'precio_mayor_registrado': precio_mayor,
                'is_menor': True
            })

            actualizacion = {
                'precio': precio_menor,
                'ultimo_costo': costo_unitario,
            }
            
            if precio_mayor > 0:
                actualizacion['volumen_precio'] = precio_mayor
                actualizacion['volumen_cantidad'] = cantidad_mayor
            else:
                actualizacion['volumen_precio'] = firestore.DELETE_FIELD
                actualizacion['volumen_cantidad'] = firestore.DELETE_FIELD

            db.collection('productos').document(producto_id).update(actualizacion)

            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def login_pos(request):
    return render(request, 'login.html')