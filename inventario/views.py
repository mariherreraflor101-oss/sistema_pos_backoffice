import json
import firebase_admin
from firebase_admin import credentials, firestore
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from datetime import datetime, timedelta, timezone 
from django.views.decorators.csrf import csrf_exempt

# Inicializar Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-credentials.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================================
# ESCUDO ARQUITECTÓNICO: Parseo de números a prueba de fallos
# ==========================================================
def to_float(val, default=0.0):
    """Convierte cualquier valor a float manejando campos vacíos y comas."""
    if val is None: 
        return default
    val_str = str(val).strip().replace(',', '.')
    if not val_str: 
        return default
    try: 
        return float(val_str)
    except ValueError: 
        return default

# ==========================================================
# NUEVO TRADUCTOR MULTI-IDIOMA PARA COMPATIBILIDAD FLUTTER/DJANGO
# ==========================================================
def obtener_valor_flexible(data, claves, default=0.0):
    """
    Busca un valor probando múltiples sinónimos (Compatibilidad Flutter vs Django).
    Resuelve el problema de variables vacías o con nombres distintos sin perder historial viejo.
    """
    for clave in claves:
        val = data.get(clave)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return default

def gestionar_compras(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.POST.get('ajax') == 'true'

        if action == 'actualizar_nombre':
            try:
                producto_id = request.POST.get('producto_id')
                nuevo_nombre = request.POST.get('nuevo_nombre', '').strip().upper()
                if producto_id and nuevo_nombre:
                    db.collection('productos').document(producto_id).update({'nombre': nuevo_nombre})
                    db.collection('auditoria_productos').add({
                        'tipo': 'CAMBIO_NOMBRE',
                        'producto_nombre': nuevo_nombre,
                        'detalle': f'Nombre corregido a: {nuevo_nombre}',
                        'usuario': 'Admin Web',
                        'fecha': firestore.SERVER_TIMESTAMP,
                    })
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

                # NUEVO: Lector de la Lista de Bonos Externos Múltiples
                bonos_externos_json = request.POST.get('bonos_externos_json')
                bonos_externos = json.loads(bonos_externos_json) if bonos_externos_json else []
                
                bonificacion_interna = 0.0
                valor_descuento_externo = 0.0

                # Cálculo de descuentos acumulados por regalos
                if len(bonos_externos) > 0:
                    for b in bonos_externos:
                        b_cant = to_float(b.get('cantidad'))
                        b_precio = to_float(b.get('precio'))
                        valor_descuento_externo += (b_cant * b_precio)
                    costo_total_global = max(0.0, costo_total_global - valor_descuento_externo)
                else:
                    bonificacion_interna = to_float(request.POST.get('bonificacion'))

                unidades_base_totales = sum(to_float(p.get('cantidad')) * to_float(p.get('factor'), 1.0) for p in lote)
                unidades_reales_totales = unidades_base_totales + bonificacion_interna
                costo_unitario_real = costo_total_global / unidades_reales_totales if unidades_reales_totales > 0 else 0.0

                # Guardado de productos base
                for p in lote:
                    p_id = p.get('id')
                    p_nombre = p.get('nombre')
                    p_cant = to_float(p.get('cantidad'))
                    p_factor = to_float(p.get('factor'), 1.0)
                    p_precio_anterior = to_float(p.get('precio_anterior'))

                    unidades_fila = p_cant * p_factor
                    bono_proporcional = (unidades_fila / unidades_base_totales) * bonificacion_interna if unidades_base_totales > 0 else 0.0
                    costo_proporcional = costo_unitario_real * (unidades_fila + bono_proporcional)

                    db.collection('compras_inventario').add({
                        'producto_id': p_id,
                        'producto_nombre': p_nombre,
                        'fecha': firestore.SERVER_TIMESTAMP,
                        'cantidad_paquetes': p_cant,
                        'factor': p_factor,
                        'bonificacion': bono_proporcional, 
                        'costo_total': costo_proporcional,
                        'costo_unitario': costo_unitario_real,
                        'precio_menor_registrado': precio_menor,
                        'precio_mayor_registrado': precio_mayor,
                        'is_menor': True,
                        'activo': True
                    })

                    db.collection('productos').document(p_id).update({
                        'precio': precio_menor,
                        'volumen_precio': precio_mayor,
                        'ultimo_costo': costo_unitario_real,
                    })

                    if p_precio_anterior != precio_menor:
                        db.collection('auditoria_productos').add({
                            'tipo': 'CAMBIO_PRECIO',
                            'producto_nombre': p_nombre,
                            'detalle': f'Precio LOTE antes: S/ {p_precio_anterior:.2f}   ➔   Nuevo: S/ {precio_menor:.2f}',
                            'usuario': 'Admin Web',
                            'fecha': firestore.SERVER_TIMESTAMP,
                        })

                # NUEVO: Guardado de cada Bono Externo de la lista
                if len(bonos_externos) > 0:
                    for b in bonos_externos:
                        b_id = b.get('id')
                        b_nombre = b.get('nombre')
                        b_cant = to_float(b.get('cantidad'))
                        b_precio = to_float(b.get('precio'))
                        
                        if b_cant > 0:
                            db.collection('compras_inventario').add({
                                'producto_id': b_id,
                                'producto_nombre': f"🎁 REGALO: {b_nombre}",
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
                producto_nombre = request.POST.get('producto_nombre')
                tipo_configuracion = request.POST.get('tipo_configuracion')
                
                datos_actualizar = {}
                
                if tipo_configuracion == 'paquete':
                    datos_actualizar['paquete_nombre'] = request.POST.get('paquete_nombre', '').strip()
                    datos_actualizar['paquete_cantidad'] = int(to_float(request.POST.get('factor_paquete'), 1.0))
                    datos_actualizar['paquete_codigo'] = request.POST.get('paquete_codigo', '').strip()
                    mensaje = f'🟡 Regla de Paquete para {producto_nombre} guardada.'
                else:
                    # GUARDADO DEL NIVEL 1
                    datos_actualizar['volumen_nombre'] = request.POST.get('volumen_nombre', '').strip()
                    datos_actualizar['volumen_cantidad'] = int(to_float(request.POST.get('cantidad_minima'), 0.0))
                    datos_actualizar['volumen_precio'] = to_float(request.POST.get('precio_volumen_oferta'))
                    
                    # GUARDADO DEL NIVEL 2
                    cant_2 = int(to_float(request.POST.get('cantidad_minima_2'), 0.0))
                    if cant_2 > 0:
                        datos_actualizar['volumen_nombre_2'] = request.POST.get('volumen_nombre_2', '').strip()
                        datos_actualizar['volumen_cantidad_2'] = cant_2
                        datos_actualizar['volumen_precio_2'] = to_float(request.POST.get('precio_volumen_oferta_2'))
                    else:
                        datos_actualizar['volumen_nombre_2'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_cantidad_2'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_precio_2'] = firestore.DELETE_FIELD

                    # GUARDADO DEL NIVEL 3
                    cant_3 = int(to_float(request.POST.get('cantidad_minima_3'), 0.0))
                    if cant_3 > 0:
                        datos_actualizar['volumen_nombre_3'] = request.POST.get('volumen_nombre_3', '').strip()
                        datos_actualizar['volumen_cantidad_3'] = cant_3
                        datos_actualizar['volumen_precio_3'] = to_float(request.POST.get('precio_volumen_oferta_3'))
                    else:
                        datos_actualizar['volumen_nombre_3'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_cantidad_3'] = firestore.DELETE_FIELD
                        datos_actualizar['volumen_precio_3'] = firestore.DELETE_FIELD

                    mensaje = f'🟠 Ofertas por Volumen para {producto_nombre} guardadas.'

                db.collection('productos').document(producto_id).update(datos_actualizar)
                if is_ajax: return JsonResponse({'status': 'success', 'message': mensaje})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error de configuración: {str(e)}'})
        elif action == 'crear_producto':
            try:
                nombre = request.POST.get('nuevo_nombre', '').strip().upper()
                codigo_barras = request.POST.get('nuevo_codigo_barras', '').strip()
                es_granel = request.POST.get('nuevo_es_granel') == 'on'
                
                precio_menor = to_float(request.POST.get('nuevo_precio_menor'))
                precio_mayor = to_float(request.POST.get('nuevo_precio_mayor'))
                
                nuevo_producto = {
                    'nombre': nombre,
                    'codigo_barras': codigo_barras,
                    'venta_granel': es_granel,
                    'precio': precio_menor,             
                    'volumen_precio': precio_mayor,     
                    'ventas_mes': 0,           
                    'stock_infinito': True,    
                    'fecha_creacion': firestore.SERVER_TIMESTAMP
                }
                
                time, doc_ref = db.collection('productos').add(nuevo_producto)
                
                detalle_auditoria = 'Registrado desde panel Web'
                if precio_menor > 0 or precio_mayor > 0: 
                    detalle_auditoria += f' (Precios iniciales - Público: S/{precio_menor} | Mayor: S/{precio_mayor})'
                else: 
                    detalle_auditoria += ' (Sin precio)'

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
                        'message': f'🟩 ¡Producto "{nombre}" creado con éxito!',
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

        elif action == 'eliminar_producto':
            try:
                producto_id = request.POST.get('producto_id')
                producto_nombre = request.POST.get('producto_nombre', 'Producto desconocido')
                if producto_id:
                    db.collection('productos').document(producto_id).delete()
                    db.collection('auditoria_productos').add({
                        'tipo': 'ELIMINAR_PRODUCTO',
                        'producto_nombre': producto_nombre,
                        'detalle': 'Producto eliminado permanentemente desde panel Web',
                        'usuario': 'Admin Web',
                        'fecha': firestore.SERVER_TIMESTAMP,
                    })
                    if is_ajax: return JsonResponse({'status': 'success', 'message': f'🗑️ El producto "{producto_nombre}" fue eliminado correctamente.', 'producto_eliminado_id': producto_id})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error al eliminar: {str(e)}'})

        elif action == 'filtrar_historial':
            try:
                fecha_desde_str = request.POST.get('fecha_desde')
                fecha_hasta_str = request.POST.get('fecha_hasta')
                if not fecha_desde_str or not fecha_hasta_str: raise ValueError("Ambas fechas son obligatorias")
                fecha_desde = datetime.strptime(fecha_desde_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                fecha_hasta = (datetime.strptime(fecha_hasta_str, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)

                compras_ref = db.collection('compras_inventario')\
                    .where('fecha', '>=', fecha_desde)\
                    .where('fecha', '<', fecha_hasta)\
                    .order_by('fecha', direction=firestore.Query.DESCENDING)\
                    .limit(500).stream()

                historial_filtrado = []
                for doc in compras_ref:
                    data = doc.to_dict()
                    fecha_obj = data.get('fecha')
                    fecha_str = fecha_obj.strftime("%d/%m/%Y") if fecha_obj else "Sin fecha"

                    historial_filtrado.append({
                        'compra_id': doc.id,
                        'producto_id': data.get('producto_id', ''),
                        'producto': data.get('producto_nombre', ''),
                        # APLICANDO EL TRADUCTOR A LOS FILTROS POST
                        'cantidad': obtener_valor_flexible(data, ['cantidad_paquetes', 'cantidad_ingresada_paquetes', 'cantidad'], 0),
                        'factor': obtener_valor_flexible(data, ['factor', 'factor_unidades', 'peso'], 1),
                        'bonificacion': obtener_valor_flexible(data, ['bonificacion', 'bono'], 0),
                        'total_pagado': obtener_valor_flexible(data, ['costo_total'], 0),
                        'precio_mayor': obtener_valor_flexible(data, ['precio_mayor_registrado', 'precio_mayor'], 0),
                        'precio_menor': obtener_valor_flexible(data, ['precio_menor_registrado', 'precio_menor'], 0),
                        'is_menor': data.get('is_menor', True), 
                        'fecha': fecha_str,  # <--- FALTABA ESTA COMA AQUÍ
                        'activo': data.get('activo', True) # <--- AÑADIR ESTA LÍNEA
                    })
                if is_ajax: return JsonResponse({'status': 'success', 'historial': historial_filtrado})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error al filtrar fechas: {str(e)}'})

        elif action == 'actualizar_proyeccion':
            try:
                compra_id = request.POST.get('compra_id')
                is_menor = request.POST.get('is_menor') == 'true'
                if compra_id:
                    db.collection('compras_inventario').document(compra_id).update({'is_menor': is_menor})
                    if is_ajax: return JsonResponse({'status': 'success'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'actualizar_precios_historial':
            try:
                compra_id = request.POST.get('compra_id')
                producto_id = request.POST.get('producto_id')
                producto_nombre = request.POST.get('producto_nombre', 'Producto')
                
                p_mayor = to_float(request.POST.get('precio_mayor'))
                p_menor = to_float(request.POST.get('precio_menor'))
                cantidad = to_float(request.POST.get('cantidad'))
                factor = to_float(request.POST.get('factor'), 1.0)
                costo_total = to_float(request.POST.get('costo_total'))
                es_granel = request.POST.get('es_granel') == 'true'
                bono = to_float(request.POST.get('bono')) 

                unidades_totales = (cantidad * factor) + bono
                costo_unitario = costo_total / unidades_totales if unidades_totales > 0 else 0.0

                if compra_id:
                    db.collection('compras_inventario').document(compra_id).update({
                        'cantidad_paquetes': cantidad,
                        'factor': factor,
                        'costo_total': costo_total,
                        'costo_unitario': costo_unitario,
                        'precio_mayor_registrado': p_mayor,
                        'precio_menor_registrado': p_menor
                    })
                
                if producto_id:
                    db.collection('productos').document(producto_id).update({
                        'venta_granel': es_granel,
                        'volumen_precio': p_mayor,
                        'precio': p_menor,
                        'ultimo_costo': costo_unitario
                    })
                    
                    db.collection('auditoria_productos').add({
                        'tipo': 'CORRECCION_COMPRA',
                        'producto_nombre': producto_nombre,
                        'detalle': f'Compra Corregida. Cant: {cantidad} | Costo: S/{costo_total:.2f} | Menor: S/{p_menor:.2f}',
                        'usuario': 'Admin Web',
                        'fecha': firestore.SERVER_TIMESTAMP,
                    })

                if is_ajax: return JsonResponse({'status': 'success', 'message': f'✅ Compra de {producto_nombre} corregida con éxito.'})
            except Exception as e:
                if is_ajax: return JsonResponse({'status': 'error', 'message': f'❌ Error al actualizar: {str(e)}'})

        if not is_ajax: return redirect('gestionar_compras')

    # ---- MÉTODO GET: TRAER DATOS INICIALES ----
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
            'volumen_nombre': data.get('volumen_nombre', ''),
            'volumen_cantidad': data.get('volumen_cantidad', ''),
            'volumen_precio_oferta': data.get('volumen_precio', ''),
            'venta_granel': data.get('venta_granel', False)
        })
    productos_json = json.dumps(lista_productos)

    compras_ref = db.collection('compras_inventario').order_by('fecha', direction=firestore.Query.DESCENDING).limit(15).stream()
    historial = []
    for doc in compras_ref:
        data = doc.to_dict()
        fecha_obj = data.get('fecha')
        fecha_str = fecha_obj.strftime("%d/%m/%Y") if fecha_obj else "Sin fecha"
        historial.append({
            'compra_id': doc.id,
            'producto_id': data.get('producto_id', ''),
            'producto': data.get('producto_nombre', ''),
            
            # APLICANDO EL TRADUCTOR A LA PANTALLA PRINCIPAL
            'cantidad': obtener_valor_flexible(data, ['cantidad_paquetes', 'cantidad_ingresada_paquetes', 'cantidad'], 0),
            'factor': obtener_valor_flexible(data, ['factor', 'factor_unidades', 'peso'], 1),
            'bonificacion': obtener_valor_flexible(data, ['bonificacion', 'bono'], 0),
            'total_pagado': obtener_valor_flexible(data, ['costo_total'], 0),
            'precio_mayor': obtener_valor_flexible(data, ['precio_mayor_registrado', 'precio_mayor'], 0),
            'precio_menor': obtener_valor_flexible(data, ['precio_menor_registrado', 'precio_menor'], 0),
            
            'is_menor': data.get('is_menor', True), 
            'fecha': fecha_str
        })

    return render(request, 'compras.html', {'productos_json': productos_json, 'historial': historial})


# ==========================================================
# APIS EXCLUSIVAS PARA LA APLICACIÓN FLUTTER
# ==========================================================

def api_historial_compras(request):
    """ API 1: Envía el historial de compras a la App Flutter """
    compras_ref = db.collection('compras_inventario').order_by('fecha', direction=firestore.Query.DESCENDING).limit(100).stream()
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