import os
import time
import io
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

# Variables de Entorno (Cargadas desde GitHub Secrets)
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

headers_mp = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}


def obtener_concepto_real(source_id, row_csv):
    """
    Consulta el detalle por source_id en /v1/payments/{source_id}.
    - Si existe (200 OK): Retorna la descripción comercial del producto o local.
    - Si falla o da 404: Retorna 'Transferencia' (payout) o 'Pago Mercadopago' (payment).
    """
    description_csv = str(row_csv.get("DESCRIPTION"))

    if pd.notna(source_id) and str(source_id).strip() != "":
        try:
            source_id_int = int(float(source_id))
            url_detalle = f"https://api.mercadopago.com/v1/payments/{source_id_int}"
            res = requests.get(url_detalle, headers=headers_mp, timeout=4)

            if res.status_code == 200:
                data = res.json()

                # 1. Obtener el título del producto/servicio en additional_info.items
                items = data.get("additional_info", {}).get("items", [])
                if items and len(items) > 0 and items[0].get("description"):
                    return items[0].get("description").strip()

                # 2. Si no hay items, tomar la descripción raíz si no es genérica
                desc_api = data.get("description")
                if desc_api and desc_api.strip() and desc_api.lower() not in ["bank transfer", "payment", "payout"]:
                    return desc_api.strip()

                # 3. Descriptor del resumen bancario o de tarjeta
                statement = data.get("statement_descriptor")
                if statement and statement.strip():
                    return statement.strip()

        except Exception as e:
            print(f"Excepción al consultar source_id {source_id}: {e}")

    # Fallback si da 404 Not Found o la consulta no trae datos válidos
    if description_csv == "payout":
        return "Transferencia"
    elif description_csv == "payment":
        return "Pago Mercadopago"

    return "Pago Mercadopago"


def ejecutar_cron():
    # 1. Definir rango de fechas del día anterior (UTC-3 / Hora Argentina) sin milisegundos
    tz_arg = timezone(timedelta(hours=-3))
    ayer = datetime.now(tz_arg) - timedelta(days=1)

    begin_date = ayer.strftime("%Y-%m-%dT03:00:00Z")
    end_date = (ayer + timedelta(days=1)).strftime("%Y-%m-%dT02:59:59Z")

    print(f"Solicitando reporte para el rango: {begin_date} a {end_date}")

    # 2. Solicitar creación del reporte a Mercado Pago
    url_reporte = "https://api.mercadopago.com/v1/account/release_report"
    payload = {
        "begin_date": begin_date,
        "end_date": end_date
    }

    res = requests.post(url_reporte, headers=headers_mp, json=payload)

    # 200, 201 y 202 (Accepted) son respuestas exitosas
    if res.status_code not in [200, 201, 202]:
        print(f"Error HTTP {res.status_code} al solicitar el reporte:", res.text)
        return

    data_reporte = res.json()
    reporte_id = data_reporte.get("id")
    print(f"Reporte solicitado con éxito (Status {res.status_code}). ID de solicitud: {reporte_id}. Esperando procesamiento...")

    # 3. Esperar disponibilidad del archivo en /list (Polling de 2 minutos)
    download_url = None
    max_intentos = 24  # 24 intentos x 5 segundos = 120 segundos

    for intento in range(1, max_intentos + 1):
        time.sleep(5)
        print(f"Verificando disponibilidad del reporte... (Intento {intento}/{max_intentos})")

        res_list = requests.get("https://api.mercadopago.com/v1/account/release_report/list", headers=headers_mp)

        if res_list.status_code == 200:
            reportes = res_list.json()

            if reportes and len(reportes) > 0:
                ultimo_reporte = reportes[0]  # El más reciente generado

                mismo_inicio = ultimo_reporte.get("begin_date") == begin_date
                mismo_fin = ultimo_reporte.get("end_date") == end_date
                estado = ultimo_reporte.get("status")

                if mismo_inicio and mismo_fin:
                    print(f"Reporte encontrado (ID Final: {ultimo_reporte.get('id')}) | Estado: '{estado}'")

                    if estado in ["enabled", "ready"]:
                        file_name = ultimo_reporte.get("file_name")
                        download_url = f"https://api.mercadopago.com/v1/account/release_report/download/{file_name}"
                        print(f"¡Reporte listo para descargar! Archivo: {file_name}")
                        break

    if not download_url:
        print("El reporte no estuvo listo después de 2 minutos de espera.")
        return

    # 4. Descargar y procesar el CSV
    print(f"Descargando reporte desde {download_url}...")
    res_csv = requests.get(download_url, headers=headers_mp)
    df = pd.read_csv(io.StringIO(res_csv.text), sep=";")

    # Limpieza inicial: eliminar filas sin fecha o sin SOURCE_ID
    df_clean = df.dropna(subset=["DATE", "SOURCE_ID"]).copy()

    # Filtrar ÚNICAMENTE egresos (NET_DEBIT_AMOUNT > 0) y que sean 'payment' o 'payout'
    tipos_validos = ["payment", "payout"]
    df_egresos = df_clean[
        (df_clean["NET_DEBIT_AMOUNT"] > 0) &
        (df_clean["DESCRIPTION"].isin(tipos_validos))
    ].copy()

    movimientos = []
    fecha_actual_iso = datetime.now(timezone.utc).isoformat()

    for _, row in df_egresos.iterrows():
        fecha = str(row["DATE"])
        source_id = row.get("SOURCE_ID")
        monto = abs(float(row["NET_DEBIT_AMOUNT"]))

        # Obtener concepto buscando por la API o aplicando fallbacks
        concepto = obtener_concepto_real(source_id, row)

        movimientos.append({
            "fecha": fecha,
            "tipo": "GASTO",
            "subtipo": "SC",
            "concepto": concepto,
            "metodo": "EFECTIVO",
            "monto": monto,
            "created_by": "6cb85359-2d2c-45c4-b4ef-7ca5953f777e",
            "created_at": fecha_actual_iso,
            "cuota_actual": 1,
            "total_cuotas": 1
        })

    print(f"Total de egresos filtrados a insertar: {len(movimientos)}")

    if not movimientos:
        print("No se encontraron egresos válidos para registrar hoy.")
        return

    # 5. Inserción en Supabase
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    response = supabase.table("movimientos").insert(movimientos).execute()
    print("Registros insertados exitosamente en Supabase:", response)


if __name__ == "__main__":
    ejecutar_cron()