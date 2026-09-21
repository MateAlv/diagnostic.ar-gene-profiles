# Base de Datos de Genes y Fenotipos Clínicos (`genes_v4.db`)


---

## 1. Origen y Fuentes de Datos

La información proviene de la consolidación de 5.228 genes humanos mapeados a partir de relaciones fenotipo-gen (`phenotype_to_genes.txt`):
* **NCBI Gene (Entrez E-utilities):** Identificadores oficiales, nombres completos, localización citogenética, resúmenes funcionales y referencias cruzadas.
* **UniProtKB:** Descripción funcional de proteínas y patologías asociadas revisadas manualmente.
* **OMIM (Online Mendelian Inheritance in Man):** Identificadores numéricos de fenotipos y enfermedades mendelianas.


---

## 2. Descripción de Columnas (Esquema Relacional)

La base de datos cuenta con una tabla principal `genes` optimizada para consultas de baja latencia indexada por el símbolo oficial del gen:

```sql
CREATE TABLE genes (
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
);

CREATE INDEX idx_simbolo ON genes(simbolo);
```

| Campo | Tipo | Descripción |
| :--- | :--- | :--- |
| `simbolo` | `TEXT` | Símbolo oficial del gen aprobado por HGNC (Primary Key). |
| `nombre_completo` | `TEXT` | Nombre formal y descriptivo del gen (en inglés según NCBI). |
| `cromosoma` | `TEXT` | Cromosoma humano donde se aloja el gen (ej. `12`, `14`, `X`). |
| `ubicacion` | `TEXT` | Localización citogenética de banda (ej. `12q13.13`, `14q32.33`). |
| `aliases` | `TEXT` | Sinónimos y nomenclaturas históricas separadas por comas. |
| `resumen_es` | `TEXT` | Resumen genético traducido y curado al español. |
| `funcion_proteina_es` | `TEXT` | Función molecular y biológica de la proteína en español. |
| `ncbi_id` | `TEXT` | Identificador único numérico en NCBI Entrez Gene. |
| `uniprot_accession` | `TEXT` | Código primario de acceso a la proteína en UniProtKB. |
| `raw_json` | `TEXT` | Objeto JSON íntegro con la estructura jerárquica y bilingüe completa. |

---

## 3. Estructura del Objeto JSON (`raw_json`)

Cada registro contiene en su columna `raw_json` la jerarquía bilingüe completa en el orden de atributos estandarizado:

```json
{
  "simbolo": "AAAS",
  "resumen_es": "La proteína codificada por este gen es miembro de la familia de proteínas reguladoras de repetición WD y puede estar involucrada en el desarrollo normal del sistema nervioso central y periférico...",
  "funcion_proteina_es": "Desempeña un papel en el desarrollo normal del sistema nervioso central y periférico. Requerido para la localización correcta de la aurora quinasa AURKA...",
  "enfermedades_asociadas_es": [
    {
      "nombre": "Síndrome de acalasia-addisonianismo-alacrima",
      "descripcion_es": "Un trastorno autosómico recesivo caracterizado por insuficiencia suprarrenal resistente a la hormona adrenocorticotrópica (ACTH), acalasia del cardias esofágico y alacrima...",
      "omim": "231550"
    }
  ],
  "nombre_completo": "aladin WD repeat nucleoporin",
  "cromosoma": "12",
  "ubicacion": "12q13.13",
  "aliases": "AAA, AAASb, ADRACALA, ADRACALIN, ALADIN, GL003",
  "resumen": "The protein encoded by this gene is a member of the WD-repeat family of regulatory proteins and may be involved in normal development of the peripheral and central nervous system...",
  "funcion_proteina": "Plays a role in the normal development of the peripheral and central nervous system. Required for the correct localization of aurora kinase AURKA...",
  "enfermedades_asociadas": [
    {
      "nombre": "Achalasia-addisonianism-alacrima syndrome",
      "descripcion": "An autosomal recessive disorder characterized by adreno-corticotropic hormone (ACTH)-resistant adrenal failure, achalasia of the esophageal cardia and alacrima...",
      "omim": "231550"
    }
  ],
  "codigo_omim": [
    "605378"
  ],
  "referencias": {
    "NCBI Gene": "[https://www.ncbi.nlm.nih.gov/gene/8086](https://www.ncbi.nlm.nih.gov/gene/8086)",
    "UniProt": "[https://www.uniprot.org/uniprotkb/Q9NRG9](https://www.uniprot.org/uniprotkb/Q9NRG9)"
  }
}
```