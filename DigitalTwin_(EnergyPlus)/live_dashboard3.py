import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import os
import time
import json
from datetime import datetime
import numpy as np

# --- CONFIGURATION ---
st.set_page_config(
    page_title="Operational Digital Twin",
    layout="wide",
    page_icon="📡"
)

BASE_DIR = "live_data"
OBJ_PATH = "thesis_room.obj"

TREND_WINDOW = 15
RECENT_POINTS = 240

# --- HELPER: GET LATEST SESSION ---
def get_latest_session():
    if not os.path.exists(BASE_DIR):
        return None

    sessions = [
        os.path.join(BASE_DIR, d)
        for d in os.listdir(BASE_DIR)
        if d.startswith("session_")
    ]

    if not sessions:
        return None

    return max(sessions, key=os.path.getmtime)

# --- CUSTOM .OBJ PARSER ---
@st.cache_data
def load_obj(filename):
    vertices, regular_faces, window_faces, door_faces = [], [], [], []

    if not os.path.exists(filename):
        return None, None, None, None

    current_material = ""

    with open(filename, "r") as f:
        for line in f:
            if line.startswith("usemtl "):
                current_material = line.strip().split()[1]

            elif line.startswith("v "):
                parts = line.strip().split()
                vertices.append([
                    float(parts[1]),
                    float(parts[2]),
                    float(parts[3])
                ])

            elif line.startswith("f "):
                parts = line.strip().split()
                face = [int(p.split("/")[0]) - 1 for p in parts[1:]]

                if "Window" in current_material:
                    target_list = window_faces
                elif "Door" in current_material:
                    target_list = door_faces
                else:
                    target_list = regular_faces

                if len(face) == 3:
                    target_list.append(face)
                elif len(face) > 3:
                    for i in range(1, len(face) - 1):
                        target_list.append([face[0], face[i], face[i + 1]])

    return vertices, regular_faces, window_faces, door_faces

def get_thermal_color(temp):
    if temp <= 22:
        return "#0055FF"
    if temp <= 24:
        return "#00FFFF"
    if temp <= 26:
        return "#00FF55"
    if temp <= 28:
        return "#FFDD00"
    return "#FF0000"

def draw_real_room(temp):
    vertices, regular_faces, window_faces, door_faces = load_obj(OBJ_PATH)

    if vertices is None:
        fig = go.Figure()
        fig.update_layout(title="⚠️ thesis_room.obj not found in folder!")
        return fig

    x_orig, y_orig, z_orig = zip(*vertices)

    x = [-val for val in x_orig]
    y = y_orig
    z = z_orig

    meshes = []

    if regular_faces:
        meshes.append(go.Mesh3d(
            x=x,
            y=z,
            z=y,
            i=[f[0] for f in regular_faces],
            j=[f[1] for f in regular_faces],
            k=[f[2] for f in regular_faces],
            color=get_thermal_color(temp),
            opacity=0.8,
            flatshading=True,
            lighting=dict(
                ambient=0.6,
                diffuse=0.8,
                specular=0.2
            )
        ))

    if window_faces:
        meshes.append(go.Mesh3d(
            x=x,
            y=z,
            z=y,
            i=[f[0] for f in window_faces],
            j=[f[1] for f in window_faces],
            k=[f[2] for f in window_faces],
            color="#88CCFF",
            opacity=0.3,
            flatshading=True,
            lighting=dict(
                ambient=0.8,
                diffuse=0.2,
                specular=1.0
            )
        ))

    if door_faces:
        meshes.append(go.Mesh3d(
            x=x,
            y=z,
            z=y,
            i=[f[0] for f in door_faces],
            j=[f[1] for f in door_faces],
            k=[f[2] for f in door_faces],
            color="#5C4033",
            opacity=1.0,
            flatshading=True,
            lighting=dict(
                ambient=0.6,
                diffuse=0.8,
                specular=0.2
            )
        ))

    fig = go.Figure(data=meshes)

    fig.update_layout(
        scene=dict(
            xaxis_visible=False,
            yaxis_visible=False,
            zaxis_visible=False,
            camera=dict(
                eye=dict(x=1.8, y=1.5, z=0.8)
            )
        ),
        margin=dict(l=0, r=0, b=0, t=0),
        height=400,
        showlegend=False
    )

    return fig

def fetch_live_data(session_path):
    csv_path = os.path.join(session_path, "live_data.csv")

    if os.path.exists(csv_path):
        try:
            return pd.read_csv(csv_path)
        except Exception:
            return pd.DataFrame()

    return pd.DataFrame()

def fetch_instant_memory(session_path):
    json_path = os.path.join(session_path, "live_memory.json")

    default_data = {
        "workshop_temp": "--",
        "workshop_rh": "--",
        "setpoint": "--",
        "occupancy": "--",
        "ac_mode": "--",
        "room_pmv": "--"
    }

    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                data = json.load(f)

            for key, value in default_data.items():
                data.setdefault(key, value)

            return data

        except Exception:
            return default_data

    return default_data

def safe_metric_value(value, decimals=1, suffix=""):
    try:
        if pd.isna(value):
            return "N/A"
        return f"{float(value):.{decimals}f}{suffix}"
    except Exception:
        return "N/A"

# --- HEADER ---
st.title("📡 Live Operational Digital Twin")

placeholder = st.empty()

while True:
    latest_session = get_latest_session()

    if not latest_session:
        with placeholder.container():
            st.warning("Waiting for Engine to start a session...")

        time.sleep(2)
        continue

    df = fetch_live_data(latest_session)

    if not df.empty and "sim_ac_kWh_1min" in df.columns:
        df["real_timestamp"] = pd.to_datetime(df["real_timestamp"])
        df["hour_group"] = df["real_timestamp"].dt.floor("h")
        df["hour_only"] = df["real_timestamp"].dt.hour
        df["date_only"] = df["real_timestamp"].dt.date

        # Make dashboard safe for older CSVs without PMV columns
        if "actual_pmv" not in df.columns:
            df["actual_pmv"] = np.nan

        if "sim_pmv" not in df.columns:
            df["sim_pmv"] = np.nan

        latest = df.iloc[-1]
        today = latest["date_only"]

        # Read latest MQTT memory early so operational status can use AC mode and occupancy
        instant_data = fetch_instant_memory(latest_session)

        current_hour_mask = df["hour_group"] == latest["hour_group"]
        cur_df = df[current_hour_mask]

        cur_hr_sim_kwh = cur_df["sim_ac_kWh_1min"].sum()
        cur_hr_act_kwh = cur_df["actual_ac_kWh_1min"].sum()
        cur_hr_temp = cur_df["sim_temp_C"].mean()

        # Daily energy aggregation uses all data recorded today, because the
        # operational twin can run outside standard office hours.
        day_mask = df["date_only"] == today
        day_df = df[day_mask]

        if not day_df.empty:
            shift_sim_kwh = day_df["sim_ac_kWh_1min"].sum()
            shift_act_kwh = day_df["actual_ac_kWh_1min"].sum()
        else:
            shift_sim_kwh = 0.0
            shift_act_kwh = 0.0

        try:
            live_occupancy = float(instant_data.get("occupancy", 0))
        except Exception:
            live_occupancy = 0.0

        live_ac_mode = str(instant_data.get("ac_mode", "")).strip().upper()
        is_ac_running = live_ac_mode not in ["OFF", "--", "NONE", "N/A", ""]
        is_occupied = live_occupancy > 0

        if is_ac_running or is_occupied:
            shift_status = "Operational"
        else:
            shift_status = "Idle"

        with placeholder.container():
            st.markdown(
                f"##### 🕒 {datetime.now().strftime('%H:%M:%S')} | "
                f"**Session:** `{os.path.basename(latest_session)}`"
            )

            st.markdown("**📡 1. Live Boundary & Controls (MQTT)**")

            i1, i2, i3, i4, i5 = st.columns(5)

            i1.metric("Workshop Temp", f"{instant_data.get('workshop_temp', '--')} °C")
            i2.metric("Workshop RH", f"{instant_data.get('workshop_rh', '--')} %")
            i3.metric("AC Setpoint", f"{instant_data.get('setpoint', '--')} °C")
            i4.metric("Occupancy", f"{instant_data.get('occupancy', '--')} People")
            i5.metric("AC Mode", f"{instant_data.get('ac_mode', '--')}")

            st.markdown("**🏢 2. Digital Twin Tracking (Simulated vs. Actual)**")

            s1, s2, s3, s4, s5 = st.columns(5)

            s1.metric(
                "Simulated Temp",
                safe_metric_value(latest["sim_temp_C"], 1, " °C"),
                f"Actual: {safe_metric_value(latest['actual_temp_C'], 1, ' °C')}",
                delta_color="off"
            )

            s2.metric(
                "Simulated RH",
                safe_metric_value(latest["sim_rh_percent"], 1, " %"),
                f"Actual: {safe_metric_value(latest['actual_rh_percent'], 1, ' %')}",
                delta_color="off"
            )

            s3.metric(
                "Simulated PMV",
                safe_metric_value(latest["sim_pmv"], 2, ""),
                f"Actual: {safe_metric_value(latest['actual_pmv'], 2, '')}",
                delta_color="off"
            )

            if shift_status == "Operational":
                s4.metric(
                    "Simulated Today AC",
                    f"{shift_sim_kwh:.2f} kWh",
                    f"Actual: {shift_act_kwh:.2f} kWh",
                    delta_color="off"
                )
                s5.success(f"Status: **{shift_status}**")
            else:
                s4.metric(
                    "Simulated Today AC",
                    "N/A",
                    "Actual: N/A",
                    delta_color="off"
                )
                s5.info(f"Status: **{shift_status}**")

            st.divider()

            col_left, col_right = st.columns([1, 2])

            with col_left:
                st.markdown("### 🧊 Virtual Thermal Model")
                st.plotly_chart(
                    draw_real_room(latest["sim_temp_C"]),
                    use_container_width=True,
                    key=f"3d_plot_{time.time()}"
                )
                st.info(
                    f"Coloring based on Simulated State: "
                    f"**{latest['sim_temp_C']:.1f}°C**"
                )

            with col_right:
                st.markdown("### 📈 Live Reality vs. Simulation")

                recent_df = df.tail(RECENT_POINTS).copy()

                recent_df["actual_temp_smooth"] = (
                    recent_df["actual_temp_C"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["sim_temp_smooth"] = (
                    recent_df["sim_temp_C"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["actual_rh_smooth"] = (
                    recent_df["actual_rh_percent"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["sim_rh_smooth"] = (
                    recent_df["sim_rh_percent"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["actual_pmv_smooth"] = (
                    recent_df["actual_pmv"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["sim_pmv_smooth"] = (
                    recent_df["sim_pmv"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["actual_ac_smooth"] = (
                    recent_df["actual_ac_kWh_1min"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                recent_df["sim_ac_smooth"] = (
                    recent_df["sim_ac_kWh_1min"]
                    .rolling(window=TREND_WINDOW, min_periods=1)
                    .mean()
                )

                tab1, tab2, tab3, tab4 = st.tabs([
                    "🌡️ Temperature",
                    "💧 Humidity",
                    "😊 PMV",
                    "⚡ AC Energy"
                ])

                with tab1:
                    fig_temp = go.Figure()

                    fig_temp.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["actual_temp_smooth"],
                        mode="lines",
                        name="Actual Measured Temp",
                        line=dict(color="blue", width=2)
                    ))

                    fig_temp.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["sim_temp_smooth"],
                        mode="lines",
                        name="Simulated Digital Twin Temp",
                        line=dict(color="orange", width=2, dash="dash")
                    ))

                    fig_temp.update_layout(
                        title="Room Temperature (Time Series)",
                        yaxis_title="°C",
                        margin=dict(l=0, r=0, t=40, b=0),
                        legend=dict(
                            orientation="h",
                            yanchor="bottom",
                            y=1.02,
                            xanchor="right",
                            x=1
                        )
                    )

                    st.plotly_chart(
                        fig_temp,
                        use_container_width=True,
                        key=f"temp_chart_{time.time()}"
                    )

                with tab2:
                    fig_rh = go.Figure()

                    fig_rh.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["actual_rh_smooth"],
                        mode="lines",
                        name="Actual Measured RH",
                        line=dict(color="teal", width=2)
                    ))

                    fig_rh.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["sim_rh_smooth"],
                        mode="lines",
                        name="Simulated Digital Twin RH",
                        line=dict(color="red", width=2, dash="dash")
                    ))

                    fig_rh.update_layout(
                        title="Room Relative Humidity (Time Series)",
                        yaxis_title="%",
                        margin=dict(l=0, r=0, t=40, b=0),
                        legend=dict(
                            orientation="h",
                            yanchor="bottom",
                            y=1.02,
                            xanchor="right",
                            x=1
                        )
                    )

                    st.plotly_chart(
                        fig_rh,
                        use_container_width=True,
                        key=f"rh_chart_{time.time()}"
                    )

                with tab3:
                    fig_pmv = go.Figure()

                    fig_pmv.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["actual_pmv_smooth"],
                        mode="lines",
                        name="Actual Measured PMV",
                        line=dict(color="yellow", width=2)
                    ))

                    fig_pmv.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["sim_pmv_smooth"],
                        mode="lines",
                        name="Simulated Digital Twin PMV",
                        line=dict(color="lightgreen", width=2, dash="dash")
                    ))

                    fig_pmv.update_layout(
                        title="Thermal Comfort PMV (Time Series)",
                        yaxis_title="PMV",
                        margin=dict(l=0, r=0, t=40, b=0),
                        legend=dict(
                            orientation="h",
                            yanchor="bottom",
                            y=1.02,
                            xanchor="right",
                            x=1
                        )
                    )

                    st.plotly_chart(
                        fig_pmv,
                        use_container_width=True,
                        key=f"pmv_chart_{time.time()}"
                    )

                with tab4:
                    fig_ac = go.Figure()

                    fig_ac.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["actual_ac_smooth"],
                        mode="lines",
                        name="Actual AC Trend",
                        line=dict(color="green", width=2)
                    ))

                    fig_ac.add_trace(go.Scatter(
                        x=recent_df["real_timestamp"],
                        y=recent_df["sim_ac_smooth"],
                        mode="lines",
                        name="Simulated AC Trend",
                        line=dict(color="purple", width=2, dash="dash")
                    ))

                    fig_ac.update_layout(
                        title="AC Energy (1-Minute Trend)",
                        yaxis_title="kWh/min",
                        margin=dict(l=0, r=0, t=40, b=0),
                        legend=dict(
                            orientation="h",
                            yanchor="bottom",
                            y=1.02,
                            xanchor="right",
                            x=1
                        )
                    )

                    st.plotly_chart(
                        fig_ac,
                        use_container_width=True,
                        key=f"ac_chart_{time.time()}"
                    )

    time.sleep(2)