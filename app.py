import re

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Carga de Datos - Plan Médico Bella Vista", layout="wide")

# =========================================================
# Login básico (contraseña única, guardada en secrets)
# =========================================================


def check_password():
    def password_entered():
        if st.session_state["password"] == st.secrets["app"]["password"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct"):
        return True

    st.text_input("Contraseña", type="password", on_change=password_entered, key="password")
    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("Contraseña incorrecta.")
    return False


if not check_password():
    st.stop()

# =========================================================
# Conexión a MySQL (Aiven)
# =========================================================


@st.cache_resource
def get_engine():
    cfg = st.secrets["mysql"]
    url = (
        f"mysql+pymysql://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
    )
    # Aiven requiere SSL. pymysql lo negocia solo si le pasamos ssl={}.
    return create_engine(url, pool_pre_ping=True, connect_args={"ssl": {"ssl": {}}})


engine = get_engine()


def get_existing_tables():
    with engine.connect() as conn:
        result = conn.execute(text("SHOW TABLES"))
        return [row[0] for row in result]


# =========================================================
# Utilidades: inferencia de tipos y normalización de nombres
# =========================================================

TYPE_OPTIONS = ["VARCHAR(255)", "TEXT", "BIGINT", "DECIMAL(14,2)", "DATE", "DATETIME"]


def infer_sql_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DECIMAL(14,2)"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "DATE"
    try:
        lengths = series.dropna().astype(str).map(len)
        max_len = int(lengths.max()) if len(lengths) else 50
    except Exception:
        max_len = 50
    if max_len >= 400:
        return "TEXT"
    return f"VARCHAR({max(50, min(max_len * 2, 400))})"


def sanitize_name(name: str) -> str:
    name = str(name).strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "col"
    if name[0].isdigit():
        name = f"c_{name}"
    return name


# =========================================================
# UI
# =========================================================

st.title("📊 Carga de Datos — Plan Médico Bella Vista")
st.caption(
    "Sube un archivo Excel o CSV, revisa la vista previa, elige a dónde va y cárgalo a la base de datos."
)

uploaded_file = st.file_uploader("Archivo Excel o CSV", type=["csv", "xlsx", "xls"])

if uploaded_file:
    try:
        if uploaded_file.name.lower().endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"No se pudo leer el archivo: {e}")
        st.stop()

    st.subheader("1. Vista previa")
    st.dataframe(df.head(20), use_container_width=True)
    st.caption(f"{len(df)} filas · {len(df.columns)} columnas detectadas en el archivo.")

    st.subheader("2. ¿Qué columnas quieres cargar?")
    cols_to_include = st.multiselect(
        "Desmarca las columnas que NO quieras subir a la base (ej. SSN u otro dato sensible).",
        options=list(df.columns),
        default=list(df.columns),
    )
    if not cols_to_include:
        st.warning("Selecciona al menos una columna para continuar.")
        st.stop()
    df = df[cols_to_include]

    st.subheader("3. ¿Dónde quieres cargar estos datos?")
    mode = st.radio(
        "Destino",
        ["Crear una tabla nueva", "Usar una tabla existente"],
        label_visibility="collapsed",
    )

    existing_tables = get_existing_tables()
    table_name = None
    col_defs = []  # (nombre_original, nombre_final, tipo_sql) — solo para modo "tabla nueva"

    if mode == "Usar una tabla existente":
        if not existing_tables:
            st.warning("Todavía no hay tablas creadas. Elige 'Crear una tabla nueva'.")
            st.stop()
        table_name = st.selectbox("Tabla existente", existing_tables)
        st.caption(
            "Los nombres de columna del archivo deben coincidir con los nombres de columna de la tabla."
        )

    else:
        raw_name = st.text_input("Nombre de la tabla nueva (ej. datos_agentes_2026)")
        table_name = sanitize_name(raw_name) if raw_name else None
        if raw_name and table_name and table_name != raw_name.strip().lower():
            st.info(f"El nombre se ajustará a: `{table_name}` (solo minúsculas, sin espacios).")

        st.markdown("**Define los atributos (columnas) de la tabla nueva:**")
        for col in df.columns:
            c1, c2 = st.columns([2, 2])
            with c1:
                col_name = st.text_input(
                    f"Nombre de columna para «{col}»",
                    value=sanitize_name(col),
                    key=f"name_{col}",
                )
            with c2:
                suggested = infer_sql_type(df[col])
                default_index = TYPE_OPTIONS.index(suggested) if suggested in TYPE_OPTIONS else 0
                col_type = st.selectbox(
                    f"Tipo de dato para «{col}»",
                    TYPE_OPTIONS,
                    index=default_index,
                    key=f"type_{col}",
                )
            col_defs.append((col, sanitize_name(col_name), col_type))

    st.divider()

    if st.button("Cargar a la base de datos", type="primary"):
        if not table_name:
            st.error("Falta el nombre de la tabla.")
            st.stop()

        try:
            with engine.begin() as conn:
                if mode == "Crear una tabla nueva":
                    cols_sql = ", ".join(f"`{cname}` {ctype}" for _, cname, ctype in col_defs)
                    create_sql = (
                        f"CREATE TABLE IF NOT EXISTS `{table_name}` ("
                        f"id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, "
                        f"{cols_sql}, "
                        f"created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                        f")"
                    )
                    conn.execute(text(create_sql))
                    rename_map = {orig: cname for orig, cname, _ in col_defs}
                    df = df.rename(columns=rename_map)

                df.to_sql(table_name, con=conn, if_exists="append", index=False)

            st.success(f"✅ {len(df)} filas cargadas en la tabla `{table_name}`.")
            st.balloons()
        except Exception as e:
            st.error(f"Error al cargar los datos: {e}")

else:
    st.info("Sube un archivo para comenzar.")
    with st.expander("Tablas que ya existen en la base"):
        try:
            tables = get_existing_tables()
            st.write(tables if tables else "Todavía no hay tablas.")
        except Exception as e:
            st.error(f"No se pudo conectar a la base de datos: {e}")
