import json
import re
import sqlite3
import time
from pathlib import Path
import requests

# --- RUTAS LOCALES ---
CARPETA_JSONS = Path(r"")
OUTPUT_DB = Path(r"")


def limpiar_referencias(texto):
    if not texto:
        return ""
    texto = texto.replace('\n', ' ')
    texto = re.sub(r'\s*\(PubMed:[^)]+\)', '', texto)
    texto = re.sub(r'\s*\(By similarity\)', '', texto)
    texto = re.sub(r'\s*\(Ref\.[^)]+\)', '', texto)
    texto = re.sub(r'"', '', texto)
    texto = re.sub(r'\s+\.', '.', texto)
    return re.sub(r'\s+', ' ', texto).strip()


def traducir_oracion(oracion):
    """Traduce mediante endpoint HTTP directo con reintentos para no ser bloqueado."""
    if not oracion or not oracion.strip():
        return ""
    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=es&dt=t&q={requests.utils.quote(oracion.strip())}"
    for _ in range(3):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                partes = r.json()[0]
                return "".join([p[0] for p in partes if p and p[0]])
        except Exception:
            time.sleep(1)
    return oracion


def traducir_texto_completo(texto):
    """Divide textos extensos por oraciones para asegurar traducción al 100%."""
    if not texto or not texto.strip():
        return ""
    # Si es un texto corto (< 200 caracteres), traduce directo
    if len(texto) < 200:
        return traducir_oracion(texto)

    
    oraciones = re.split(r'(?<=\.)\s+', texto)
    traducciones = []
    bloque = ""

    for o in oraciones:
        if len(bloque) + len(o) < 300:
            bloque += (" " + o if bloque else o)
        else:
            if bloque:
                traducciones.append(traducir_oracion(bloque))
                time.sleep(0.1)
            bloque = o

    if bloque:
        traducciones.append(traducir_oracion(bloque))

    return " ".join(traducciones)


def reparar_y_compilar():
    if not CARPETA_JSONS.exists():
        print(f"[ERROR] No existe la carpeta: {CARPETA_JSONS}")
        return

    archivos = list(CARPETA_JSONS.glob("*.json"))
    total = len(archivos)
    print(f"Archivos encontrados: {total}. Revisando datos y armando base de datos...")

    
    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    conn = sqlite3.connect(OUTPUT_DB)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS genes (
            simbolo TEXT PRIMARY KEY,
            nombre_completo TEXT,
            cromosoma TEXT,
            ubicacion TEXT,
            aliases TEXT,
            resumen_es TEXT,
            funcion_proteina_es TEXT,
            ncbi_id TEXT,
            uniprot_accession TEXT,
            raw_json TEXT NOT NULL
        )
    """)

    lote = []
    tamano_lote = 500

    for idx, archivo in enumerate(archivos, start=1):
        simbolo = archivo.stem.strip().upper()
        hubo_cambios = False

        with open(archivo, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                continue

        
        resumen_en = data.get("resumen", "")
        resumen_es = data.get("resumen_es", "")
        funcion_en = data.get("funcion_proteina", "")
        funcion_es = data.get("funcion_proteina_es", "")
        enfermedades = data.get("enfermedades_asociadas", [])
        enfermedades_es = data.get("enfermedades_asociadas_es", [])

        
        necesita_traduccion = (
            "error" in data or
            (resumen_en and (not resumen_es or resumen_es == resumen_en)) or
            ("AKT has an important role" in funcion_es) or
            (not funcion_es and funcion_en)
        )

        if necesita_traduccion:
            print(f" -> Corrigiendo traducción pendiente de: {simbolo}...")
            resumen_es = traducir_texto_completo(resumen_en)
            funcion_es = traducir_texto_completo(funcion_en)

            # Traducir enfermedades y limpiar vacías
            enfermedades_limpias_es = []
            for enf in enfermedades:
                nom = enf.get("nombre", "").strip()
                desc = enf.get("descripcion", "").strip()
                omim = enf.get("omim", "").strip()
                if not nom and not desc:
                    continue
                enfermedades_limpias_es.append({
                    "nombre": traducir_texto_completo(nom),
                    "descripcion_es": traducir_texto_completo(desc),
                    "omim": omim
                })

            data["simbolo"] = simbolo
            data["resumen_es"] = resumen_es
            data["funcion_proteina_es"] = funcion_es
            data["enfermedades_asociadas_es"] = enfermedades_limpias_es
            if "error" in data:
                del data["error"]
            hubo_cambios = True

        
        if hubo_cambios:
            with open(archivo, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        # Preparar fila para SQLite
        refs = data.get("referencias", {})
        ncbi_id = refs.get("NCBI Gene", "").split("/")[-1] if refs.get("NCBI Gene") else ""
        uniprot_acc = refs.get("UniProt", "").split("/")[-1] if refs.get("UniProt") else ""

        lote.append((
            simbolo,
            data.get("nombre_completo", ""),
            data.get("cromosoma", ""),
            data.get("ubicacion", ""),
            data.get("aliases", ""),
            resumen_es,
            funcion_es,
            ncbi_id,
            uniprot_acc,
            json.dumps(data, ensure_ascii=False)
        ))

        if len(lote) >= tamano_lote:
            cursor.executemany("""
                INSERT OR REPLACE INTO genes (
                    simbolo, nombre_completo, cromosoma, ubicacion, aliases,
                    resumen_es, funcion_proteina_es, ncbi_id, uniprot_accession, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, lote)
            conn.commit()
            lote.clear()
            print(f"Indexados {idx}/{total} genes...")

    if lote:
        cursor.executemany("""
            INSERT OR REPLACE INTO genes (
                simbolo, nombre_completo, cromosoma, ubicacion, aliases,
                resumen_es, funcion_proteina_es, ncbi_id, uniprot_accession, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, lote)
        conn.commit()

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_simbolo ON genes(simbolo)")
    conn.commit()
    conn.close()

    print("\n" + "=" * 45)
    print("¡Base de datos y archivos actualizados!")
    print(f"Ubicación: {OUTPUT_DB}")
    print("=" * 45)


if __name__ == "__main__":
    reparar_y_compilar()