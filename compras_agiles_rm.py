#!/usr/bin/env python3
"""
compras_agiles_rm.py

Descarga las Compras Ágiles abiertas (estado "publicada") de la Región
Metropolitana desde la API oficial de Mercado Público (Compra Ágil v2),
las clasifica por categoría, calcula días restantes para el cierre y
señala aquellas con riesgo de quedar desiertas por falta de oferentes.

Requiere un ticket de acceso a la API de Mercado Público:
  https://www.chilecompra.cl/api/  -> "Pide tu ticket" (login con Clave Única)

Uso:
    export COMPRA_AGIL_TICKET="tu-ticket-aqui"
    python3 compras_agiles_rm.py

    # o pasando el ticket directamente:
    python3 compras_agiles_rm.py --ticket TU_TICKET

    # otros estados (por defecto solo "publicada" = abiertas):
    python3 compras_agiles_rm.py --estado publicada,proveedor_seleccionado

Salidas (en la carpeta actual):
    compras_agiles_rm.csv   -> datos en bruto, una fila por Compra Ágil
    compras_agiles_rm.html  -> reporte visual agrupado por categoría
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from collections import defaultdict

import requests

BASE_URL = "https://api2.mercadopublico.cl"
REGION_METROPOLITANA = 13

# --- Clasificación heurística por palabras clave -----------------------
# La API no entrega un campo oficial de "categoría", así que se infiere
# desde el nombre del proceso. Ajusta este diccionario a tu rubro.
CATEGORIAS = {
    "Aseo y Limpieza": ["aseo", "limpieza", "sanitiz", "detergente", "desinfect"],
    "Alimentación y Bebestibles": ["alimento", "colación", "colacion", "coffee break",
                                    "bebestible", "catering", "fruta", "verdura",
                                    "carne", "abarrote"],
    "Oficina y Papelería": ["papel", "oficina", "escritorio", "tóner", "toner",
                             "tinta", "cartucho", "resma"],
    "Mobiliario": ["mobiliario", "silla", "estante", "mueble", "mesa"],
    "Tecnología e Informática": ["computador", "notebook", "software", "licencia",
                                  "impresora", "informátic", "informatic",
                                  "tecnológic", "tecnologic", "servidor", "router",
                                  "proyector"],
    "Ferretería y Construcción": ["ferretería", "ferreteria", "construcción",
                                   "construccion", "pintura", "cemento",
                                   "eléctrico", "electrico", "gasfitería",
                                   "gasfiteria", "herramienta", "material"],
    "Vestuario, Textil y EPP": ["vestuario", "uniforme", "ropa", "calzado", "epp",
                                 "elemento de protección", "elemento de proteccion",
                                 "guante", "mascarilla"],
    "Salud e Insumos Médicos": ["médico", "medico", "salud", "insumo clínico",
                                 "insumo clinico", "farmac", "medicamento",
                                 "hospitalario"],
    "Vehículos, Combustible y Transporte": ["vehículo", "vehiculo", "combustible",
                                             "neumático", "neumatico", "transporte",
                                             "arriendo de vehículo", "flete"],
    "Mantención y Reparaciones": ["mantención", "mantencion", "reparación",
                                   "reparacion", "mantenimiento", "climatización",
                                   "climatizacion"],
    "Servicios Profesionales y Consultorías": ["consultoría", "consultoria",
                                                "asesoría", "asesoria",
                                                "capacitación", "capacitacion",
                                                "servicio profesional"],
    "Eventos y Difusión": ["evento", "difusión", "difusion", "ceremonia",
                            "gigantografía", "gigantografia", "pendón", "pendon"],
    "Jardinería y Áreas Verdes": ["jardín", "jardin", "áreas verdes",
                                   "areas verdes", "poda", "riego"],
}


def clasificar(nombre: str) -> str:
    nombre_low = (nombre or "").lower()
    for categoria, palabras in CATEGORIAS.items():
        if any(p in nombre_low for p in palabras):
            return categoria
    return "Otros / Sin clasificar"


def get_ticket(args_ticket):
    ticket = args_ticket or os.environ.get("COMPRA_AGIL_TICKET")
    if not ticket:
        sys.exit("Falta el ticket. Usa --ticket TU_TICKET o define COMPRA_AGIL_TICKET.")
    return ticket


def request_con_reintentos(url, headers, params, max_intentos=5, timeout=60):
    """Hace un GET reintentando ante timeouts / errores transitorios del servidor
    (502/503/504), con espera creciente entre intentos. Los errores de cliente
    (400/401/403/404) no se reintentan, ya que reintentar no los soluciona."""
    for intento in range(1, max_intentos + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            if intento == max_intentos:
                raise
            espera = min(2 ** intento, 30)
            print(
                f"  Error de red ({exc}). Reintentando en {espera}s "
                f"(intento {intento}/{max_intentos})...",
                file=sys.stderr,
            )
            time.sleep(espera)
            continue

        if resp.status_code in (502, 503, 504):
            if intento == max_intentos:
                return resp
            espera = min(2 ** intento, 30)
            print(
                f"  HTTP {resp.status_code} (servidor ocupado). Reintentando en "
                f"{espera}s (intento {intento}/{max_intentos})...",
                file=sys.stderr,
            )
            time.sleep(espera)
            continue

        return resp
    return resp


def fetch_compras_agiles(ticket, region=REGION_METROPOLITANA, estado="publicada"):
    items = []
    pagina = 1
    while True:
        params = {
            "region": region,
            "estado": estado,
            "tamano_pagina": 50,
            "numero_pagina": pagina,
            "ordenar_por": "FechaPublicacion",
        }
        resp = request_con_reintentos(
            f"{BASE_URL}/v2/compra-agil",
            headers={"ticket": ticket},
            params=params,
        )
        if resp.status_code == 429:
            print("Cuota diaria agotada (429). Intenta más tarde.", file=sys.stderr)
            break
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") != "OK":
            print("Error de la API:", data.get("errors"), file=sys.stderr)
            break
        payload = data["payload"]
        items.extend(payload["items"])
        paginacion = payload["paginacion"]
        print(
            f"  página {paginacion['numero_pagina']}/{paginacion['total_paginas']} "
            f"({paginacion['total_resultados']} resultados totales)"
        )
        if paginacion["numero_pagina"] >= paginacion["total_paginas"]:
            break
        pagina += 1
        time.sleep(0.2)
    return items


def dias_restantes(fecha_cierre_iso):
    if not fecha_cierre_iso:
        return None
    try:
        cierre = datetime.fromisoformat(fecha_cierre_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if cierre.tzinfo is None:
        # La API a veces devuelve fechas sin zona horaria explícita; se asume UTC.
        cierre = cierre.replace(tzinfo=timezone.utc)
    ahora = datetime.now(timezone.utc)
    return (cierre - ahora).total_seconds() / 86400


def build_rows(items):
    rows = []
    for it in items:
        nombre = it.get("nombre", "")
        fecha_cierre = it.get("fechas", {}).get("fecha_cierre")
        dias = dias_restantes(fecha_cierre)
        ofertas = it.get("resumen", {}).get("total_ofertas_recibidas", 0) or 0
        riesgo = dias is not None and dias <= 1 and ofertas == 0
        rows.append(
            {
                "id": it.get("codigo"),
                "categoria": clasificar(nombre),
                "nombre": nombre,
                "organismo": it.get("institucion", {}).get("organismo_comprador"),
                "fecha_publicacion": it.get("fechas", {}).get("fecha_publicacion"),
                "fecha_cierre": fecha_cierre,
                "dias_restantes": round(dias, 1) if dias is not None else None,
                "presupuesto_clp": it.get("montos", {}).get("monto_disponible_clp"),
                "ofertas_recibidas": ofertas,
                "riesgo_desierta": "SI" if riesgo else "",
                "estado": it.get("estado", {}).get("glosa"),
                "link_detalle": it.get("links", {}).get("detalle"),
            }
        )
    rows.sort(
        key=lambda r: (
            r["categoria"],
            r["dias_restantes"] if r["dias_restantes"] is not None else 999,
        )
    )
    return rows


def write_csv(rows, path="compras_agiles_rm.csv"):
    campos = [
        "id", "categoria", "nombre", "organismo", "fecha_publicacion",
        "fecha_cierre", "dias_restantes", "presupuesto_clp",
        "ofertas_recibidas", "riesgo_desierta", "estado", "link_detalle",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV guardado en {path}")


def write_html(rows, path="compras_agiles_rm.html"):
    por_categoria = defaultdict(list)
    for r in rows:
        por_categoria[r["categoria"]].append(r)

    total_riesgo = sum(1 for r in rows if r["riesgo_desierta"])
    generado = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Agregados por categoría, usados para las opciones del filtro y para
    # poder ordenar las categorías por cantidad / presupuesto / riesgo.
    agregados = {}
    for categoria, filas in por_categoria.items():
        agregados[categoria] = {
            "cantidad": len(filas),
            "presupuesto": sum(f["presupuesto_clp"] or 0 for f in filas),
            "riesgo": sum(1 for f in filas if f["riesgo_desierta"]),
        }

    opciones_filtro = ['<option value="todas">Todas las categorías</option>']
    for categoria in sorted(por_categoria):
        opciones_filtro.append(
            f'<option value="{categoria}">{categoria} '
            f'({agregados[categoria]["cantidad"]})</option>'
        )

    html = [f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Compras Ágiles abiertas - Región Metropolitana</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#fafafa; color:#222; }}
h1 {{ font-size: 1.4rem; }}
h2 {{ font-size: 1.05rem; margin-bottom: 0.3rem; }}
.meta {{ color:#666; margin-bottom: 1rem; }}
.controls {{ display:flex; gap:1.5rem; flex-wrap:wrap; margin-bottom:1.5rem;
             background:#fff; padding:0.75rem 1rem; border:1px solid #ddd; border-radius:6px; }}
.controls label {{ font-size:0.85rem; color:#333; }}
.controls select {{ margin-left:0.4rem; padding:3px 6px; font-size:0.85rem; }}
.categoria {{ margin-top: 2rem; }}
table {{ border-collapse: collapse; width: 100%; background:#fff; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; font-size: 0.82rem; text-align:left; vertical-align:top; }}
th {{ background:#f0f0f0; }}
tr.riesgo {{ background:#fff2f2; }}
.badge {{ background:#c0392b; color:#fff; padding:2px 6px; border-radius:4px; font-size:0.72rem; white-space:nowrap; }}
</style></head><body>
<h1>Compras Ágiles abiertas — Región Metropolitana</h1>
<div class="meta">Generado: {generado} · Total procesos: {len(rows)} · Con riesgo de deserción (cierran en &lt;24h, 0 ofertas): {total_riesgo}</div>
<div class="controls">
  <label>Filtrar categoría:
    <select id="filtroCategoria">
      {''.join(opciones_filtro)}
    </select>
  </label>
  <label>Ordenar categorías por:
    <select id="ordenCategoria">
      <option value="alfabetico">Alfabético</option>
      <option value="cantidad">Cantidad de procesos</option>
      <option value="presupuesto">Presupuesto total</option>
      <option value="riesgo">Procesos en riesgo</option>
    </select>
  </label>
</div>
<div id="contenedorCategorias">
"""]

    for categoria in sorted(por_categoria):
        filas = por_categoria[categoria]
        agg = agregados[categoria]
        html.append(
            f'<div class="categoria" data-categoria="{categoria}" '
            f'data-cantidad="{agg["cantidad"]}" data-presupuesto="{agg["presupuesto"]}" '
            f'data-riesgo="{agg["riesgo"]}"><h2>{categoria} ({len(filas)})</h2><table>'
        )
        html.append(
            "<tr><th>ID</th><th>Detalle</th><th>Organismo</th><th>Cierre</th>"
            "<th>Días rest.</th><th>Presupuesto (CLP)</th><th>Ofertas</th><th></th></tr>"
        )
        for r in filas:
            css_row = ' class="riesgo"' if r["riesgo_desierta"] else ""
            badge = '<span class="badge">sin oferentes</span>' if r["riesgo_desierta"] else ""
            presupuesto = f"${r['presupuesto_clp']:,.0f}" if r["presupuesto_clp"] else "-"
            html.append(
                f"<tr{css_row}><td>{r['id']}</td><td>{r['nombre']}</td>"
                f"<td>{r['organismo']}</td><td>{r['fecha_cierre']}</td>"
                f"<td>{r['dias_restantes']}</td><td>{presupuesto}</td>"
                f"<td>{r['ofertas_recibidas']}</td><td>{badge}</td></tr>"
            )
        html.append("</table></div>")

    html.append("</div>")  # cierre de #contenedorCategorias

    script_js = """
<script>
  var filtro = document.getElementById('filtroCategoria');
  var orden = document.getElementById('ordenCategoria');
  var contenedor = document.getElementById('contenedorCategorias');

  function aplicarFiltro() {
    var val = filtro.value;
    document.querySelectorAll('.categoria').forEach(function (div) {
      div.style.display = (val === 'todas' || div.dataset.categoria === val) ? '' : 'none';
    });
  }

  function aplicarOrden() {
    var criterio = orden.value;
    var divs = Array.from(contenedor.querySelectorAll('.categoria'));
    divs.sort(function (a, b) {
      if (criterio === 'cantidad') return b.dataset.cantidad - a.dataset.cantidad;
      if (criterio === 'presupuesto') return b.dataset.presupuesto - a.dataset.presupuesto;
      if (criterio === 'riesgo') return b.dataset.riesgo - a.dataset.riesgo;
      return a.dataset.categoria.localeCompare(b.dataset.categoria, 'es');
    });
    divs.forEach(function (div) { contenedor.appendChild(div); });
  }

  filtro.addEventListener('change', aplicarFiltro);
  orden.addEventListener('change', aplicarOrden);
</script>
"""
    html.append(script_js)
    html.append("</body></html>")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"HTML guardado en {path}")


def main():
    parser = argparse.ArgumentParser(description="Reporte de Compras Ágiles abiertas en la RM")
    parser.add_argument("--ticket", help="Ticket de la API de Mercado Público")
    parser.add_argument(
        "--estado",
        default="publicada",
        help="Estado(s) a consultar, separados por coma (default: publicada)",
    )
    args = parser.parse_args()

    ticket = get_ticket(args.ticket)
    print("Descargando Compras Ágiles de la Región Metropolitana...")
    items = fetch_compras_agiles(ticket, estado=args.estado)
    print(f"Total descargado: {len(items)}")

    rows = build_rows(items)
    write_csv(rows)
    write_html(rows)


if __name__ == "__main__":
    main()
