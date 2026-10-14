import json
import os
from decimal import Decimal, InvalidOperation
import requests
import firebase_admin
from firebase_admin import credentials, firestore, auth
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from datetime import datetime
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache

# ==========================================================
# SERVIDORES
# ==========================================================
URL_MAESTRO = "https://api-santarosa.bodegaelpueblo.com"
URL_LOCAL = "https://desktop-oa7vet9.tailacf1c8.ts.net"

# ==========================================================
# FIREBASE SOLO PARA USUARIOS / AUTH
# ==========================================================
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-credentials.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================================
# CACHE
# ==========================================================
CACHE_PRODUCTOS = 'pos_productos_mysql_v1'
CACHE_PRODUCTOS_STALE = 'pos_productos_mysql_stale_v1'
CACHE_USUARIOS = 'pos_usuarios_v1'
CACHE_USUARIOS_STALE = 'pos_usuarios_stale_v1'
CACHE_HISTORIAL_15 = 'pos_historial_mysql_15_v1'
CACHE_HISTORIAL_15_STALE = 'pos_historial_mysql_15_stale_v1'
CACHE_HISTORIAL_APP = 'pos_historial_mysql_app_v1'
CACHE_HISTORIAL_APP_STALE = 'pos_historial_mysql_app_stale_v1'


def invalidar_cache_productos():
    cache.delete(CACHE_PRODUCTOS)


def invalidar_cache_usuarios():
    cache.delete(CACHE_USUARIOS)


def invalidar_cache_historial():
    cache.delete(CACHE_HISTORIAL_15)
    cache.delete(CACHE_HISTORIAL_APP)


def to_float(val, default=0.0):
    if val is None:
        return default
    val_str = str(val).strip().replace(',', '.')
    if not val_str:
        return default
    try:
        return float(val_str)
    except (ValueError, TypeError):
        return default


def _json_or_error(resp):
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get('status') == 'error':
        raise RuntimeError(data.get('message') or 'Error en API local')
    return data


def local_get(path, *, params=None, timeout=12):
    return _json_or_error(requests.get(f"{URL_LOCAL}{path}", params=params, timeout=timeout))


def local_post(path, *, data=None, json_data=None, timeout=12):
    return _json_or_error(requests.post(f"{URL_LOCAL}{path}", data=data, json=json_data, timeout=timeout))


def local_patch(path, *, data=None, json_data=None, timeout=12):
    return _json_or_error(requests.patch(f"{URL_LOCAL}{path}", data=data, json=json_data, timeout=timeout))


def local_delete(path, *, timeout=12):
    return _json_or_error(requests.delete(f"{URL_LOCAL}{path}", timeout=timeout))


def sincronizar_precio_central(empresa_id, producto, precio):
    """Actualiza SOLO el precio empresarial central; exige credencial de servicio."""
    secreto = os.environ.get('POS_CENTRAL_SYNC_KEY', '').strip()
    if not secreto:
        raise RuntimeError('Falta configurar POS_CENTRAL_SYNC_KEY en el servicio POS.')
    if not empresa_id or str(empresa_id).lower() in ('null', 'none'):
        raise RuntimeError('No se recibió empresa_id válido.')
    nombre = (producto.get('nombre') or '').strip()
    if not nombre:
        raise RuntimeError('No se identificó el nombre del producto.')
    datos = {
        'accion': 'sincronizar_precios_calculadora',
        'empresa_id': str(empresa_id),
        'nombre': nombre,
        'codigo_barras': (producto.get('codigo_barras') or '').strip(),
        'precio_final': str(precio),
    }
    respuesta = requests.post(
        f'{URL_MAESTRO}/api/interno/control-superior/',
        data=datos, headers={'X-POS-Sync-Key': secreto}, timeout=10,
    )
    respuesta.raise_for_status()
    resultado = respuesta.json()
    if resultado.get('status') != 'success':
        raise RuntimeError(resultado.get('message') or 'API central no confirmó el precio')
    return resultado



def sincronizar_nombre_y_precio_central(empresa_id, nombre_anterior, nombre_nuevo, precio, *, rol='', precio_antiguo=None):
    """Sincronizacion limitada a empresa: nombre global solo con rol superadmin."""
    secreto = os.environ.get('POS_CENTRAL_SYNC_KEY', '').strip()
    if not secreto:
        raise RuntimeError('POS_CENTRAL_SYNC_KEY no configurada.')
    if not nombre_anterior or not nombre_nuevo:
        raise RuntimeError('Falta identificacion del producto.')
    datos = {'accion':'sincronizar_nombre_y_precio', 'empresa_id':str(empresa_id),
             'nombre_antiguo':nombre_anterior, 'nuevo_nombre':nombre_nuevo,
             'precio_final':str(precio), 'rol_pos':rol}
    # Solo el editor de catálogo envía este campo. Otras operaciones lo conservan.
    if precio_antiguo is not None:
        datos['precio_antiguo'] = str(precio_antiguo)
    respuesta = requests.post(
        f'{URL_MAESTRO}/api/interno/control-superior/',
        data=datos,
        headers={'X-POS-Sync-Key':secreto}, timeout=10)
    respuesta.raise_for_status()
    resultado = respuesta.json()
    if resultado.get('status') != 'success':
        raise RuntimeError(resultado.get('message') or 'API no confirmó actualización.')
    return resultado

def _producto_id_panel(p):
    return p.get('firebase_id') or p.get('id')


def _producto_local_por_id(producto_id):
    for p in obtener_productos_cache():
        if str(p.get('id')) == str(producto_id) or str(p.get('mysql_id')) == str(producto_id):
            return p
    return None


def _fecha_ddmmyyyy(valor):
    if not valor:
        return 'Sin fecha'
    try:
        s = str(valor).replace('Z', '+00:00')
        return datetime.fromisoformat(s).strftime('%d/%m/%Y')
    except Exception:
        try:
            return datetime.strptime(str(valor)[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
        except Exception:
            return str(valor)


def _compra_para_panel(c):
    return {
        'compra_id': c.get('id'),
        'producto_id': c.get('producto_firebase_id') or c.get('producto_id') or '',
        'producto': c.get('producto_nombre', ''),
        'cantidad': to_float(c.get('cantidad_paquetes')),
        'factor': to_float(c.get('factor'), 1.0),
        'bonificacion': to_float(c.get('bonificacion')),
        'total_pagado': to_float(c.get('costo_total')),
        'precio_mayor': to_float(c.get('precio_mayor_registrado')),
        'precio_menor': to_float(c.get('precio_menor_registrado')),
        'is_menor': bool(c.get('is_menor', True)),
        'fecha': _fecha_ddmmyyyy(c.get('fecha')),
        'activo': bool(c.get('activo', True)),
    }


# ==========================================================
# DATOS PARA EL PANEL - MYSQL LOCAL
# ==========================================================
def obtener_productos_cache():
    productos = cache.get(CACHE_PRODUCTOS)
    if productos is not None:
        return productos

    try:
        respuesta = local_get('/api/productos')
        lista_productos = []
        for data in respuesta.get('productos', []):
            lista_productos.append({
                'id': _producto_id_panel(data),
                'mysql_id': data.get('id'),
                'firebase_id': data.get('firebase_id'),
                'nombre': data.get('nombre', 'Sin nombre'),
                'precio_actual': to_float(data.get('precio')),
                'precio_antiguo': to_float(data.get('precio_antiguo')),
                'precio_mayor': to_float(data.get('precio_mayor', data.get('volumen_precio'))),
                'paquete_nombre': data.get('paquete_nombre') or '',
                'paquete_codigo': data.get('paquete_codigo') or '',
                'paquete_cantidad': to_float(data.get('paquete_cantidad'), 1),
                'codigo_barras': data.get('codigo_barras') or '',
                'categoria_id': data.get('categoria_id') or '',
                'subcategoria_id': data.get('subcategoria_id') or '',
                'volumen_nombre': data.get('volumen_nombre') or '',
                'volumen_cantidad': to_float(data.get('volumen_cantidad')),
                'volumen_precio_oferta': to_float(data.get('volumen_precio')),
                'volumen_nombre_2': data.get('volumen_nombre_2') or '',
                'volumen_cantidad_2': to_float(data.get('volumen_cantidad_2')),
                'volumen_precio_2': to_float(data.get('volumen_precio_2')),
                'volumen_nombre_3': data.get('volumen_nombre_3') or '',
                'volumen_cantidad_3': to_float(data.get('volumen_cantidad_3')),
                'volumen_precio_3': to_float(data.get('volumen_precio_3')),
                'venta_granel': bool(data.get('venta_granel', False)),
                'imagen': bool(data.get('tiene_imagen', False)),
                'tiene_imagen': bool(data.get('tiene_imagen', False)),
                'activo': bool(data.get('activo', True)),
                'visible_en_app': bool(data.get('visible_en_app', True)),
                'orden': data.get('orden', 9999),
                'ultimo_costo': to_float(data.get('ultimo_costo')),
                'cantidad_mayor': to_float(data.get('volumen_cantidad'), 3),
            })
        cache.set(CACHE_PRODUCTOS, lista_productos, 60)
        cache.set(CACHE_PRODUCTOS_STALE, lista_productos, 86400)
        return lista_productos
    except Exception as e:
        print(f'⚠️ Error leyendo productos desde MySQL local: {e}')
        return cache.get(CACHE_PRODUCTOS_STALE, [])


def obtener_usuarios_cache():
    usuarios = cache.get(CACHE_USUARIOS)
    if usuarios is not None:
        return usuarios
    try:
        lista = []
        for doc in db.collection('usuarios').stream():
            data = doc.to_dict()
            lista.append({
                'id': doc.id,
                'uid': doc.id,
                'nombre': data.get('nombre', 'Sin nombre'),
                'email': data.get('email', data.get('correo', 'Sin correo')),
                'rol': data.get('rol', 'CAJERO'),
                'activo': data.get('activo', True),
            })
        cache.set(CACHE_USUARIOS, lista, 300)
        cache.set(CACHE_USUARIOS_STALE, lista, 86400)
        return lista
    except Exception as e:
        print(f'⚠️ Error leyendo usuarios de Firebase: {e}')
        return cache.get(CACHE_USUARIOS_STALE, [])


def obtener_historial_15_cache():
    historial = cache.get(CACHE_HISTORIAL_15)
    if historial is not None:
        return historial
    try:
        respuesta = local_get('/api/compras', params={'limit': 15})
        lista = [_compra_para_panel(c) for c in respuesta.get('compras', [])]
        cache.set(CACHE_HISTORIAL_15, lista, 30)
        cache.set(CACHE_HISTORIAL_15_STALE, lista, 86400)
        return lista
    except Exception as e:
        print(f'⚠️ Error leyendo historial desde MySQL local: {e}')
        return cache.get(CACHE_HISTORIAL_15_STALE, [])


# ==========================================================
# PANEL COMPRAS
# ==========================================================
def gestionar_compras(request):
    # No confiar en localStorage ni en el empresa_id del formulario.
    empresa_sesion = request.session.get('pos_empresa_id')
    if not empresa_sesion:
        if request.method == 'GET':
            return redirect('/login/')
        return JsonResponse({'status': 'error', 'message': 'Sesión vencida. Inicie sesión nuevamente.'}, status=401)
    if request.method == 'POST':
        empresa_enviada = (request.POST.get('empresa_id') or '').strip()
        if empresa_enviada and empresa_enviada != str(empresa_sesion):
            return JsonResponse({'status': 'error', 'message': 'Empresa no autorizada para esta sesión.'}, status=403)
        action = request.POST.get('action')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.POST.get('ajax') == 'true'

        if action in {
            'crear_producto', 'actualizar_nombre', 'registrar_compra', 'guardar_configuracion',
            'eliminar_producto', 'guardar_orden_catalogo', 'actualizar_precios_historial',
            'editar_producto_catalogo', 'toggle_visibilidad_app', 'importar_producto_central'
        }:
            invalidar_cache_productos()
        if action in {'registrar_compra', 'actualizar_proyeccion', 'actualizar_precios_historial', 'eliminar_compra'}:
            invalidar_cache_historial()
        if action in {'crear_cajero'}:
            invalidar_cache_usuarios()

        # --------------------------
        # Puentes al VPS maestro
        # --------------------------
        if action == 'obtener_categorias':
            try:
                res = requests.get(f"{URL_MAESTRO}/api/interno/categorias/", timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                return JsonResponse({'status': 'error', 'categorias': [], 'message': str(e)})

        elif action == 'obtener_subcategorias':
            cat_id = request.POST.get('categoria_id')
            try:
                res = requests.get(f"{URL_MAESTRO}/api/interno/subcategorias/{cat_id}/", timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                return JsonResponse({'status': 'error', 'subcategorias': [], 'message': str(e)})

        elif action == 'listar_empleados_control':
            try:
                res = requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                    'accion': 'listar',
                    'empresa_id': str(empresa_sesion),
                    'rol_solicitante': request.POST.get('rol_solicitante')
                }, timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': f'Fallo de conexión con el VPS: {e}'})

        elif action == 'actualizar_empleado_control':
            try:
                res = requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                    'accion': 'actualizar',
                    'empresa_id': str(empresa_sesion),
                    'usuario_id': request.POST.get('usuario_id'),
                    'nuevo_rol': request.POST.get('nuevo_rol'),
                    'is_active': request.POST.get('is_active'),
                    'rol_solicitante': request.POST.get('rol_solicitante')
                }, timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': f'Fallo de conexión con el VPS: {e}'})

        elif action == 'actualizar_permisos_control':
            try:
                res = requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                    'accion': 'actualizar_permisos',
                    'usuario_id': request.POST.get('usuario_id'),
                    'perm_imagen': request.POST.get('perm_imagen'),
                    'perm_nombre': request.POST.get('perm_nombre'),
                    'perm_codigo': request.POST.get('perm_codigo')
                }, timeout=5)
                return JsonResponse(res.json())
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': f'Fallo de conexión con el VPS: {e}'})

        # --------------------------
        # Crear producto: local + maestro
        # --------------------------
        elif action == 'crear_producto':
            try:
                nombre = request.POST.get('nuevo_nombre', '').strip().upper()
                codigo_barras = request.POST.get('nuevo_codigo_barras', '').strip()
                es_granel = request.POST.get('nuevo_es_granel') == 'on'
                categoria_id = request.POST.get('nueva_categoria_id') or None
                subcategoria_id = request.POST.get('nueva_subcategoria_id') or None
                precio_menor = to_float(request.POST.get('nuevo_precio_menor'))
                precio_mayor = to_float(request.POST.get('nuevo_precio_mayor'))
                empresa_id = str(empresa_sesion)
                es_global = request.POST.get('es_global') == 'on'
                tiene_foto = 'nueva_imagen' in request.FILES

                creado = local_post('/api/productos', json_data={
                    'nombre': nombre,
                    'codigo_barras': codigo_barras,
                    'precio': precio_menor,
                    'precio_mayor': precio_mayor,
                    'volumen_precio': precio_mayor,
                    'categoria_id': categoria_id,
                    'subcategoria_id': subcategoria_id,
                    'venta_granel': es_granel,
                    'tiene_imagen': tiene_foto,
                    'visible_en_app': tiene_foto,
                    'activo': True,
                    'ventas_mes': 0,
                    'stock_infinito': True,
                })

                archivos = {}
                if tiene_foto:
                    img = request.FILES['nueva_imagen']
                    archivos = {'imagen': (img.name, img.read(), img.content_type)}

                datos_mysql = {
                    'nombre': nombre,
                    'codigo_barras': codigo_barras,
                    'precio_final': precio_menor,
                    'precio_antiguo': 0,
                    'categoria_id': categoria_id,
                    'subcategoria_id': subcategoria_id,
                    'empresa_id': empresa_id,
                    'venta_granel': 'true' if es_granel else 'false',
                    'es_global': 'true' if es_global else 'false',
                    'estado': 'true' if tiene_foto else 'false',
                }
                try:
                    requests.post(f"{URL_MAESTRO}/api/interno/recibir_producto/", data=datos_mysql, files=archivos, timeout=5)
                    if not tiene_foto and empresa_id:
                        requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                            'accion': 'toggle_visibilidad', 'nombre': nombre,
                            'empresa_id': empresa_id, 'estado': 'false'
                        }, timeout=3)
                except Exception as api_err:
                    print('⚠️ Producto creado localmente, pero falló sincronización al maestro:', api_err)

                nuevo_id = creado.get('firebase_id') or creado.get('id')
                if is_ajax:
                    return JsonResponse({
                        'status': 'success',
                        'message': f'🟩 Producto "{nombre}" creado en MySQL local.',
                        'nuevo_producto': {
                            'id': nuevo_id,
                            'nombre': nombre,
                            'precio_actual': precio_menor,
                            'precio_mayor': precio_mayor,
                            'paquete_nombre': '', 'paquete_codigo': '', 'paquete_cantidad': 1,
                            'volumen_nombre': '', 'volumen_cantidad': '', 'volumen_precio_oferta': '',
                            'venta_granel': es_granel,
                            'codigo_barras': codigo_barras,
                            'categoria_id': categoria_id,
                            'subcategoria_id': subcategoria_id,
                            'activo': True,
                            'visible_en_app': tiene_foto,
                        }
                    })
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': f'❌ Error al crear: {e}'})

        elif action == 'actualizar_nombre':
            try:
                producto_id = request.POST.get('producto_id')
                nuevo_nombre = request.POST.get('nuevo_nombre', '').strip().upper()
                producto = _producto_local_por_id(producto_id) if producto_id else None
                if not producto or not nuevo_nombre:
                    return JsonResponse({'status':'error','message':'Producto o nombre inválido.'}, status=400)
                antiguo = (producto.get('nombre') or '').strip()
                if nuevo_nombre == antiguo:
                    return JsonResponse({'status':'success','message':'Nombre sin cambios.', 'nombre_actualizado':nuevo_nombre})
                # Reutilizar precio actual: el mismo enlace central valida producto/empresa.
                try:
                    sincronizar_nombre_y_precio_central(str(empresa_sesion), antiguo, nuevo_nombre,
                        producto.get('precio_actual', 0), rol=request.session.get('pos_rol',''))
                except Exception as exc:
                    return JsonResponse({'status':'error','message':'API central no actualizada: '+str(exc)}, status=409)
                try:
                    local_patch(f'/api/productos/{producto_id}', json_data={'nombre':nuevo_nombre})
                except Exception as exc:
                    return JsonResponse({'status':'warning','message':'Nombre central actualizado, pero falló MySQL POS: '+str(exc)})
                return JsonResponse({'status':'success','message':'Nombre sincronizado con API y POS.', 'nombre_actualizado':nuevo_nombre})
            except Exception as exc:
                return JsonResponse({'status':'error','message':str(exc)}, status=500)

        # --------------------------
        # Registrar lote/compra
        # --------------------------
        elif action == 'registrar_compra':
            try:
                lote_str = request.POST.get('productos_lote')
                if not lote_str:
                    raise ValueError('No hay productos en el lote')
                lote = json.loads(lote_str)
                costo_total_global = to_float(request.POST.get('costo_total'))
                precio_menor = to_float(request.POST.get('precio_menor'))
                precio_mayor = to_float(request.POST.get('precio_mayor'))
                bonos_externos_json = request.POST.get('bonos_externos_json')
                bonos_externos = json.loads(bonos_externos_json) if bonos_externos_json else []
                bonificacion_interna = 0.0
                valor_descuento_externo = 0.0

                if bonos_externos:
                    for b in bonos_externos:
                        valor_descuento_externo += to_float(b.get('cantidad')) * to_float(b.get('precio'))
                    costo_total_global = max(0.0, costo_total_global - valor_descuento_externo)
                else:
                    bonificacion_interna = to_float(request.POST.get('bonificacion'))

                unidades_base_totales = sum(to_float(p.get('cantidad')) * to_float(p.get('factor'), 1.0) for p in lote)
                unidades_reales_totales = unidades_base_totales + bonificacion_interna
                costo_unitario_real = costo_total_global / unidades_reales_totales if unidades_reales_totales > 0 else 0.0

                empresa_id = str(empresa_sesion)
                errores_sincronizacion = []

                for p in lote:
                    p_id = p.get('id')
                    unidades_fila = to_float(p.get('cantidad')) * to_float(p.get('factor'), 1.0)
                    bono_proporcional = (unidades_fila / unidades_base_totales) * bonificacion_interna if unidades_base_totales > 0 else 0.0
                    costo_proporcional = costo_unitario_real * (unidades_fila + bono_proporcional)

                    local_post('/api/compras', json_data={
                        'producto_id': p_id,
                        'producto_nombre': p.get('nombre'),
                        'cantidad_paquetes': to_float(p.get('cantidad')),
                        'factor': to_float(p.get('factor'), 1.0),
                        'bonificacion': bono_proporcional,
                        'costo_total': costo_proporcional,
                        'costo_unitario': costo_unitario_real,
                        'precio_menor_registrado': precio_menor,
                        'precio_mayor_registrado': precio_mayor,
                        'is_menor': False if precio_mayor > 0 else True,
                    })

                    if empresa_id:
                        try:
                            producto_local = _producto_local_por_id(p_id) or {}
                            tiene_imagen = bool(producto_local.get('imagen', False))
                            sincronizar_precio_central(empresa_id, {
                                'nombre': producto_local.get('nombre') or p.get('nombre'),
                                'codigo_barras': producto_local.get('codigo_barras'),
                            }, precio_menor)
                            if not tiene_imagen:
                                requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                                    'accion': 'toggle_visibilidad',
                                    'nombre': p.get('nombre'),
                                    'empresa_id': empresa_id,
                                    'estado': 'false'
                                }, timeout=3)
                        except Exception as e:
                            errores_sincronizacion.append(str(p.get('nombre')))
                            print(f"⚠️ Error sincronizando precio para {p.get('nombre')}: {e}")

                for b in bonos_externos:
                    b_cant = to_float(b.get('cantidad'))
                    if b_cant > 0:
                        b_precio = to_float(b.get('precio'))
                        local_post('/api/compras', json_data={
                            'producto_id': b.get('id'),
                            'producto_nombre': f"🎁 REGALO: {b.get('nombre')}",
                            'cantidad_paquetes': b_cant,
                            'factor': 1.0,
                            'bonificacion': 0.0,
                            'costo_total': b_cant * b_precio,
                            'costo_unitario': b_precio,
                            'precio_menor_registrado': b_precio,
                            'precio_mayor_registrado': b_precio,
                            'is_menor': True,
                        })

                if is_ajax:
                    if errores_sincronizacion or not empresa_id:
                        return JsonResponse({'status': 'warning', 'message':
                            'Compra guardada localmente; sincronización central pendiente. No vuelvas a registrar el lote.',
                            'productos_pendientes': errores_sincronizacion})
                    return JsonResponse({'status': 'success', 'message': '✅ Lote guardado y precios confirmados en API central.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': f'❌ Error al registrar lote: {e}'})

        elif action == 'guardar_configuracion':
            try:
                producto_id = request.POST.get('producto_id')
                tipo = request.POST.get('tipo_configuracion')
                datos = {}
                if tipo == 'paquete':
                    datos = {
                        'paquete_nombre': request.POST.get('paquete_nombre', '').strip(),
                        'paquete_cantidad': to_float(request.POST.get('factor_paquete'), 1.0),
                        'paquete_codigo': request.POST.get('paquete_codigo', '').strip(),
                    }
                else:
                    cant_2 = to_float(request.POST.get('cantidad_minima_2'))
                    cant_3 = to_float(request.POST.get('cantidad_minima_3'))
                    datos = {
                        'volumen_nombre': request.POST.get('volumen_nombre', '').strip(),
                        'volumen_cantidad': to_float(request.POST.get('cantidad_minima')),
                        'volumen_precio': to_float(request.POST.get('precio_volumen_oferta')),
                        'volumen_nombre_2': request.POST.get('volumen_nombre_2', '').strip() if cant_2 > 0 else None,
                        'volumen_cantidad_2': cant_2 if cant_2 > 0 else 0,
                        'volumen_precio_2': to_float(request.POST.get('precio_volumen_oferta_2')) if cant_2 > 0 else 0,
                        'volumen_nombre_3': request.POST.get('volumen_nombre_3', '').strip() if cant_3 > 0 else None,
                        'volumen_cantidad_3': cant_3 if cant_3 > 0 else 0,
                        'volumen_precio_3': to_float(request.POST.get('precio_volumen_oferta_3')) if cant_3 > 0 else 0,
                    }
                local_patch(f'/api/productos/{producto_id}', json_data=datos)
                if is_ajax:
                    return JsonResponse({'status': 'success', 'message': 'Configuración guardada'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': f'❌ Error: {e}'})

        elif action == 'eliminar_producto':
            try:
                producto_id = request.POST.get('producto_id')
                producto_nombre = request.POST.get('producto_nombre')
                empresa_id = str(empresa_sesion)
                local_delete(f'/api/productos/{producto_id}')
                if empresa_id:
                    try:
                        requests.post(f"{URL_MAESTRO}/api/interno/recibir_producto/", data={
                            'nombre': producto_nombre,
                            'empresa_id': empresa_id,
                            'eliminar': 'true'
                        }, timeout=3)
                    except Exception as e:
                        print('⚠️ Error eliminando en el maestro:', e)
                if is_ajax:
                    return JsonResponse({'status': 'success', 'message': f'🟩 Producto "{producto_nombre}" desactivado.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'guardar_orden_catalogo':
            try:
                orden_json = request.POST.get('orden_json')
                if orden_json:
                    items = json.loads(orden_json)
                    local_post('/api/productos/orden', json_data={'items': items})
                    try:
                        requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                            'accion': 'guardar_orden_maestro', 'orden_json': orden_json
                        }, timeout=3)
                    except Exception as api_err:
                        print('⚠️ Error enviando orden al maestro:', api_err)
                    if is_ajax:
                        return JsonResponse({'status': 'success'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'filtrar_historial':
            try:
                respuesta = local_get('/api/compras/filtrar', params={
                    'desde': request.POST.get('fecha_desde'),
                    'hasta': request.POST.get('fecha_hasta'),
                })
                historial = [_compra_para_panel(c) for c in respuesta.get('compras', [])]
                if is_ajax:
                    return JsonResponse({'status': 'success', 'historial': historial})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'actualizar_proyeccion':
            try:
                compra_id = request.POST.get('compra_id')
                if compra_id:
                    local_patch(f'/api/compras/{compra_id}', json_data={
                        'is_menor': request.POST.get('is_menor') == 'true'
                    })
                    if is_ajax:
                        return JsonResponse({'status': 'success'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'actualizar_precios_historial':
            try:
                compra_id = request.POST.get('compra_id')
                producto_id = request.POST.get('producto_id')
                p_mayor = to_float(request.POST.get('precio_mayor'))
                p_menor = to_float(request.POST.get('precio_menor'))
                cantidad = to_float(request.POST.get('cantidad'))
                factor = to_float(request.POST.get('factor'), 1.0)
                bono = to_float(request.POST.get('bono'))
                costo_total = to_float(request.POST.get('costo_total'))
                es_granel = request.POST.get('es_granel') == 'true'

                if compra_id:
                    local_patch(f'/api/compras/{compra_id}', json_data={
                        'cantidad_paquetes': cantidad,
                        'factor': factor,
                        'bonificacion': bono,
                        'costo_total': costo_total,
                        'precio_mayor': p_mayor,
                        'precio_menor': p_menor,
                    })
                if producto_id:
                    local_patch(f'/api/productos/{producto_id}', json_data={
                        'venta_granel': es_granel,
                        'precio_mayor': p_mayor,
                        'volumen_precio': p_mayor,
                        'precio': p_menor,
                    })
                if is_ajax:
                    empresa_id = str(empresa_sesion)
                    producto_local = _producto_local_por_id(producto_id) if producto_id else None
                    try:
                        if not producto_local:
                            raise RuntimeError('Producto local no identificable; se requiere reconciliación.')
                        sincronizar_precio_central(empresa_id, producto_local, p_menor)
                    except Exception as exc:
                        return JsonResponse({'status': 'warning', 'message':
                            'Compra actualizada en MySQL local; API central pendiente: ' + str(exc)})
                    return JsonResponse({'status': 'success', 'message':
                        '✅ Compra corregida y precio confirmado en API central.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'eliminar_compra':
            try:
                compra_id = request.POST.get('compra_id')
                if compra_id:
                    local_delete(f'/api/compras/{compra_id}')
                    if is_ajax:
                        return JsonResponse({'status': 'success', 'message': '🗑️ Compra eliminada permanentemente.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        # --------------------------
        # Usuarios: se mantienen en Firebase Auth/Firestore
        # --------------------------
        elif action == 'crear_cajero':
            try:
                nombre = request.POST.get('nombre', '').strip()
                email = request.POST.get('email', '').strip()
                password = request.POST.get('password', '').strip()
                rol = request.POST.get('rol', 'CAJERO').strip().upper()
                user_record = auth.create_user(email=email, password=password, display_name=nombre)
                db.collection('usuarios').document(user_record.uid).set({
                    'nombre': nombre, 'email': email, 'correo': email,
                    'rol': rol, 'activo': True,
                    'fecha_creacion': firestore.SERVER_TIMESTAMP
                })
                if is_ajax:
                    return JsonResponse({'status': 'success', 'message': f'✅ {rol} "{nombre}" creado con éxito.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': f'❌ Error: {e}'})

        elif action == 'editar_producto_catalogo':
            try:
                producto_id = request.POST.get('producto_id')
                producto = _producto_local_por_id(producto_id) if producto_id else None
                if not producto:
                    return JsonResponse({'status': 'error', 'message': 'Producto local no encontrado.'}, status=404)

                nombre = request.POST.get('editar_nombre', '').strip().upper()
                antiguo = (producto.get('nombre') or '').strip()
                if not nombre or not antiguo:
                    return JsonResponse({'status': 'error', 'message': 'Nombre de producto inválido.'}, status=400)

                precio_menor = to_float(request.POST.get('editar_precio_menor'))
                precio_mayor = to_float(request.POST.get('editar_precio_mayor'))
                precio_antiguo = to_float(request.POST.get('editar_precio_antiguo'))
                codigo_ingresado = (request.POST.get('editar_codigo_barras') or '').strip()
                codigo_existente = str(producto.get('codigo_barras') or '').strip()
                codigo_a_guardar = codigo_ingresado or codigo_existente
                es_granel = request.POST.get('editar_es_granel') == 'on'

                # Solo sincronizar cuando realmente cambie nombre o precio.
                cambia_maestro = (
                    nombre != antiguo.upper()
                    or abs(precio_menor - to_float(producto.get('precio_actual'))) >= 0.001
                    or abs(precio_antiguo - to_float(producto.get('precio_antiguo'))) >= 0.001
                    or abs(precio_mayor - to_float(producto.get('precio_mayor'))) >= 0.001
                )
                datos_locales = {}
                if codigo_a_guardar != codigo_existente:
                    datos_locales['codigo_barras'] = codigo_a_guardar

                # Las categorías solo pertenecen al producto local. Los selects
                # deshabilitados no se envían: nunca borrar por omisión.
                if 'editar_categoria_id' in request.POST:
                    nueva_categoria = (request.POST.get('editar_categoria_id') or '').strip()
                    categoria_actual = str(producto.get('categoria_id') or '').strip()
                    if nueva_categoria and nueva_categoria != categoria_actual:
                        datos_locales['categoria_id'] = nueva_categoria
                if 'editar_subcategoria_id' in request.POST:
                    nueva_subcategoria = (request.POST.get('editar_subcategoria_id') or '').strip()
                    subcategoria_actual = str(producto.get('subcategoria_id') or '').strip()
                    if nueva_subcategoria and nueva_subcategoria != subcategoria_actual:
                        datos_locales['subcategoria_id'] = nueva_subcategoria

                if es_granel != bool(producto.get('venta_granel', False)):
                    datos_locales['venta_granel'] = es_granel

                if cambia_maestro:
                    # Validar primero el maestro, evitando cambios parciales de precio.
                    try:
                        sincronizar_nombre_y_precio_central(
                            str(empresa_sesion), antiguo, nombre, precio_menor,
                            rol=request.session.get('pos_rol', ''),
                            precio_antiguo=request.POST.get('editar_precio_antiguo', '0'))
                    except Exception as exc:
                        return JsonResponse({'status': 'error',
                                             'message': 'Cambios no aplicados: ' + str(exc)}, status=409)
                    datos_locales.update({
                        'nombre': nombre,
                        'precio': precio_menor,
                        'precio_mayor': precio_mayor,
                        'volumen_precio': precio_mayor,
                        'precio_antiguo': precio_antiguo,
                    })

                if not datos_locales:
                    return JsonResponse({'status': 'success',
                                         'message': 'No hay cambios para guardar.',
                                         'datos': {}})

                try:
                    local_patch(f'/api/productos/{producto_id}', json_data=datos_locales)
                except Exception as exc:
                    mensaje = ('La API central pudo actualizarse, pero MySQL local falló: '
                               if cambia_maestro else 'No se pudo actualizar MySQL local: ')
                    return JsonResponse({'status': 'error',
                                         'message': mensaje + str(exc)}, status=502)

                return JsonResponse({'status': 'success',
                                     'message': ('Nombre/precios sincronizados y cambios locales guardados.'
                                                 if cambia_maestro else 'Cambios guardados solo en MySQL local.'),
                                     'datos': datos_locales})
            except Exception as exc:
                return JsonResponse({'status': 'error', 'message': str(exc)}, status=500)

        elif action == 'toggle_visibilidad_app':
            try:
                producto_id = request.POST.get('producto_id')
                producto_nombre = request.POST.get('producto_nombre')
                nuevo_estado = request.POST.get('estado') == 'true'
                empresa_id = str(empresa_sesion)
                if producto_id:
                    local_patch(f'/api/productos/{producto_id}', json_data={'visible_en_app': nuevo_estado})
                if empresa_id:
                    try:
                        requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                            'accion': 'toggle_visibilidad', 'nombre': producto_nombre,
                            'empresa_id': empresa_id,
                            'estado': 'true' if nuevo_estado else 'false'
                        }, timeout=3)
                    except Exception as e:
                        print('⚠️ Error VPS Toggle:', e)
                if is_ajax:
                    return JsonResponse({'status': 'success'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': str(e)})

        elif action == 'listar_catalogo_global':
            try:
                respuesta = requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                    'accion': 'listar_catalogo_global',
                    'empresa_id': request.POST.get('empresa_id')
                }, timeout=10)
                if respuesta.status_code == 200:
                    return JsonResponse(respuesta.json())
                return JsonResponse({'status': 'error', 'message': f'Error del Servidor Central: Código {respuesta.status_code}'})
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': f'Fallo de conexión física: {e}'})

        elif action == 'importar_producto_central':
            producto_id = request.POST.get('producto_id')
            try:
                respuesta = requests.post(f"{URL_MAESTRO}/api/interno/control-superior/", data={
                    'accion': 'importar_producto_global',
                    'producto_id': producto_id,
                    'empresa_id': request.POST.get('empresa_id')
                }, timeout=10)
                if respuesta.status_code != 200:
                    return JsonResponse({'status': 'error', 'message': f'Error del Servidor Central: Código {respuesta.status_code}'})
                data_maestro = respuesta.json()
                if data_maestro.get('status') == 'success':
                    prod = data_maestro.get('nuevo_producto', {})
                    if prod:
                        local_post('/api/productos', json_data={
                            'firebase_id': str(prod.get('id')),
                            'nombre': prod.get('nombre'),
                            'codigo_barras': prod.get('codigo_barras', ''),
                            'venta_granel': prod.get('venta_granel', False),
                            'precio': 0.0,
                            'precio_mayor': 0.0,
                            'volumen_precio': 0.0,
                            'tiene_imagen': prod.get('imagen', False),
                            'visible_en_app': prod.get('imagen', False),
                            'ventas_mes': 0,
                            'stock_infinito': True,
                            'orden': prod.get('orden', 9999),
                        })
                return JsonResponse(data_maestro)
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': f'No se pudo importar: {e}'})

        if not is_ajax:
            return redirect('gestionar_compras')

    lista_productos = obtener_productos_cache()
    productos_json = json.dumps(lista_productos)
    historial = obtener_historial_15_cache()
    lista_cajeros = obtener_usuarios_cache()

    return render(request, 'compras.html', {
        'productos_json': productos_json,
        'historial': historial,
        'cajeros': lista_cajeros
    })


# ==========================================================
# APIS PARA FLUTTER - AHORA MYSQL LOCAL
# ==========================================================
def api_historial_compras(request):
    lista = cache.get(CACHE_HISTORIAL_APP)
    if lista is None:
        try:
            respuesta = local_get('/api/compras', params={'limit': 3000})
            lista = []
            for c in respuesta.get('compras', []):
                lista.append({
                    'id': c.get('id'),
                    'producto_nombre': c.get('producto_nombre', 'Desconocido'),
                    'fecha': _fecha_ddmmyyyy(c.get('fecha')),
                    'costo_total': to_float(c.get('costo_total')),
                    'cantidad': to_float(c.get('cantidad_paquetes')),
                    'factor': to_float(c.get('factor'), 1.0),
                    'bono': to_float(c.get('bonificacion')),
                    'precio_menor': to_float(c.get('precio_menor_registrado')),
                    'precio_mayor': to_float(c.get('precio_mayor_registrado')),
                    'activo': bool(c.get('activo', True)),
                })
            cache.set(CACHE_HISTORIAL_APP, lista, 60)
            cache.set(CACHE_HISTORIAL_APP_STALE, lista, 86400)
        except Exception as e:
            print(f'⚠️ Error en api_historial_compras MySQL: {e}')
            lista = cache.get(CACHE_HISTORIAL_APP_STALE, [])
    return JsonResponse({'compras': lista})


def api_buscar_productos(request):
    search_query = request.GET.get('search', '').lower().strip()
    productos = obtener_productos_cache()
    lista = []
    for data in productos:
        nombre = data.get('nombre', '')
        codigo = str(data.get('codigo_barras') or '')
        if search_query in nombre.lower() or (search_query and search_query in codigo.lower()):
            lista.append({
                'id': data.get('id'),
                'nombre': nombre,
                'venta_granel': data.get('venta_granel', False),
                'ultimo_costo': data.get('ultimo_costo', 0.0),
                'precio_menor': data.get('precio_actual', 0.0),
                'precio_mayor': data.get('precio_mayor', 0.0),
                'cantidad_mayor': data.get('cantidad_mayor', 3),
            })
    return JsonResponse({'productos': lista})


@csrf_exempt
def api_registrar_compra_app(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            cantidad = to_float(data.get('cantidad'))
            factor = to_float(data.get('factor'), 1.0)
            costo_total = to_float(data.get('costo_total'))
            precio_menor = to_float(data.get('precio_menor'))
            precio_mayor = to_float(data.get('precio_mayor'))
            unidades_totales = cantidad * factor
            costo_unitario = costo_total / unidades_totales if unidades_totales > 0 else 0.0

            resultado = local_post('/api/compras', json_data={
                'producto_id': data.get('producto_id'),
                'producto_nombre': data.get('producto_nombre', 'Producto desde App'),
                'cantidad_paquetes': cantidad,
                'factor': factor,
                'bonificacion': 0.0,
                'costo_total': costo_total,
                'costo_unitario': costo_unitario,
                'precio_menor_registrado': precio_menor,
                'precio_mayor_registrado': precio_mayor,
                'is_menor': True,
            })
            invalidar_cache_productos()
            invalidar_cache_historial()
            return JsonResponse({'status': 'success', 'id': resultado.get('id')})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    return JsonResponse({'status': 'error', 'message': 'Método no permitido'}, status=405)


def login_pos(request):
    """Login POS: identidad comprobada por API central; empresa fijada en sesión Django."""
    if request.method == 'GET':
        return render(request, 'login.html')
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Método no permitido'}, status=405)
    usuario = (request.POST.get('username') or '').strip()
    clave = request.POST.get('password') or ''
    if not usuario or not clave:
        return JsonResponse({'status': 'error', 'message': 'Usuario y contraseña requeridos.'}, status=400)
    try:
        respuesta = requests.post(
            f'{URL_MAESTRO}/api/interno/login_pos/',
            data={'username': usuario, 'password': clave}, timeout=10,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({'status': 'error', 'message': 'No se pudo validar el acceso.'}, status=502)
    if datos.get('status') != 'success':
        return JsonResponse({'status': 'error', 'message': 'Usuario o contraseña incorrectos, o sin empresa asignada.'}, status=401)
    empresa_id = datos.get('empresa_id')
    if not empresa_id or str(empresa_id).lower() in ('none', 'null'):
        return JsonResponse({'status': 'error', 'message': 'Usuario sin empresa asignada.'}, status=403)
    request.session.flush()
    request.session['pos_empresa_id'] = str(empresa_id)
    request.session['pos_usuario_nombre'] = str(datos.get('nombre_usuario') or usuario)
    request.session['pos_rol'] = str(datos.get('rol') or '')
    request.session.set_expiry(28800)
    return JsonResponse({
        'status': 'success', 'empresa_id': empresa_id,
        'empresa_nombre': datos.get('empresa_nombre', ''),
        'nombre_usuario': datos.get('nombre_usuario', usuario),
        'rol': datos.get('rol', ''),
        'perm_imagen': datos.get('perm_imagen', False),
        'perm_nombre': datos.get('perm_nombre', False),
        'perm_codigo': datos.get('perm_codigo', False),
    })


def panel_ceo(request):
    return render(request, 'panel_ceo.html')


# ==========================================================
# USUARIOS Y ROLES - SE MANTIENEN EN FIREBASE
# ==========================================================
def gestionar_usuarios(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.POST.get('ajax') == 'true'

        if action in {'crear_usuario', 'eliminar_usuario'}:
            invalidar_cache_usuarios()

        if action == 'crear_usuario':
            try:
                nombre = request.POST.get('nombre', '').strip()
                email = request.POST.get('email', '').strip()
                password = request.POST.get('password', '').strip()
                rol = request.POST.get('rol', 'cajero').upper()
                user_record = auth.create_user(email=email, password=password, display_name=nombre)
                db.collection('usuarios').document(user_record.uid).set({
                    'nombre': nombre,
                    'email': email,
                    'rol': rol,
                    'activo': True,
                    'fecha_creacion': firestore.SERVER_TIMESTAMP
                })
                if is_ajax:
                    return JsonResponse({'status': 'success', 'message': f'✅ Usuario {nombre} creado con éxito.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': f'❌ Error: {e}'})

        elif action == 'eliminar_usuario':
            try:
                uid = request.POST.get('uid')
                if uid:
                    auth.delete_user(uid)
                    db.collection('usuarios').document(uid).delete()
                    if is_ajax:
                        return JsonResponse({'status': 'success', 'message': '🗑️ Usuario eliminado del sistema.'})
            except Exception as e:
                if is_ajax:
                    return JsonResponse({'status': 'error', 'message': f'❌ Error al eliminar: {e}'})

    lista_usuarios = obtener_usuarios_cache()
    return render(request, 'usuarios.html', {'usuarios': lista_usuarios})
