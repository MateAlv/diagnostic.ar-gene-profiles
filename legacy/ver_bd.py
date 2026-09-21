import json
import sqlite3
from pathlib import Path

DB_PATH = Path(r"genes_v4.db")


def obtener_datos_gen(simbolo: str):
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT raw_json FROM genes WHERE simbolo = ?", (simbolo.upper(),)
    )
    fila = cursor.fetchone()
    conn.close()

    if not fila:
        return None

    datos = json.loads(fila[0])

    
    var_simbolo = datos.get("simbolo", "")
    var_resumen_es = datos.get("resumen_es", "")
    var_funcion_proteina_es = datos.get("funcion_proteina_es", "")
    var_enfermedades_asociadas_es = datos.get("enfermedades_asociadas_es", [])
    var_nombre_completo = datos.get("nombre_completo", "")
    var_cromosoma = datos.get("cromosoma", "")
    var_ubicacion = datos.get("ubicacion", "")
    var_aliases = datos.get("aliases", "")
    var_funcion_proteina = datos.get("funcion_proteina", "")
    var_enfermedades_asociadas = datos.get("enfermedades_asociadas", [])
    var_codigo_omim = datos.get("codigo_omim", [])
    var_referencias = datos.get("referencias", {})

    return (
        var_simbolo,
        var_resumen_es,
        var_funcion_proteina_es,
        var_enfermedades_asociadas_es,
        var_nombre_completo,
        var_cromosoma,
        var_ubicacion,
        var_aliases,
        var_funcion_proteina,
        var_enfermedades_asociadas,
        var_codigo_omim,
        var_referencias,
    )


"""
# --- PRUEBA ---
if __name__ == "__main__":
    resultado = obtener_datos_gen("AAAS")

    if resultado:
        (
            simbolo,
            resumen_es,
            funcion_proteina_es,
            enfermedades_asociadas_es,
            nombre_completo,
            cromosoma,
            ubicacion,
            aliases,
            funcion_proteina,
            enfermedades_asociadas,
            codigo_omim,
            referencias,
        ) = resultado

        # 1. Variables principales en orden
        print(f'"simbolo": "{simbolo}"')
        print(f'"resumen_es": {resumen_es}\n')
        print(f'"funcion_proteina_es": {funcion_proteina_es}\n')

        # 2. Enfermedades asociadas en español
        print('"enfermedades_asociadas_es":')
        for enf_es in enfermedades_asociadas_es:
            nom_es = enf_es.get("nombre", "")
            desc_es = enf_es.get("descripcion_es", "")
            print(f'    "nombre": {nom_es}')
            print(f'    "descripcion_es": {desc_es}\n')

        # 3. Metadatos generales y función original
        print(f'"nombre_completo": "{nombre_completo}"')
        print(f'"cromosoma": "{cromosoma}"')
        print(f'"ubicacion": "{ubicacion}"')
        print(f'"aliases": "{aliases}"')
        print(f'"funcion_proteina": {funcion_proteina}\n')

        # 4. Enfermedades asociadas en inglés
        print('"enfermedades_asociadas":')
        for enf in enfermedades_asociadas:
            nom_en = enf.get("nombre", "")
            desc_en = enf.get("descripcion", "")
            print(f'    "nombre": {nom_en}')
            print(f'    "descripcion": {desc_en}\n')

        # 5. OMIM y Referencias
        print(f'"codigo_omim": {codigo_omim}')
        print('"referencias":')
        print(f'    "NCBI Gene": {referencias.get("NCBI Gene", "")}')
        print(f'    "UniProt": {referencias.get("UniProt", "")}')
    else:
        print("El gen no fue encontrado en la base de datos.")

"""