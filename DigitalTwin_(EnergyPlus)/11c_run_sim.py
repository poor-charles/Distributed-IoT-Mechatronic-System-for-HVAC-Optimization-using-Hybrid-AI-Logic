from pathlib import Path
import pandas as pd
import subprocess
import shutil
import sys
from eppy.modeleditor import IDF

PROJECT_DIR = Path(__file__).resolve().parents[1]

ENERGYPLUS_EXE = r"C:\EnergyPlusV25-1-0\energyplus.exe"
IDD_PATH = r"C:\EnergyPlusV25-1-0\Energy+.idd"
IDF.setiddname(IDD_PATH)

BASE_IDF = PROJECT_DIR / "IDF" / "base_model.idf"
EPW_PATH = PROJECT_DIR / "Weather" / "site.epw"

PARAM_FILE = PROJECT_DIR / "calibration" / "current_params.csv"
RUN_DIR = PROJECT_DIR / "results" / "current_run"

# --------------------------------------------------
# Reset current run folder
# --------------------------------------------------
if RUN_DIR.exists():
    shutil.rmtree(RUN_DIR)
RUN_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------
# Load parameters proposed by Bayesian optimizer
# --------------------------------------------------
params = pd.read_csv(PARAM_FILE).iloc[0]

# --------------------------------------------------
# Schedule file paths
# --------------------------------------------------
occ_file = PROJECT_DIR / "calibration" / "schedules" / "occupancy_schedule_values.csv"
set_file = PROJECT_DIR / "calibration" / "schedules" / "cooling_setpoint_schedule_values.csv"
wrk_file = PROJECT_DIR / "calibration" / "schedules" / "workshop_temp_schedule_values.csv"

# --------------------------------------------------
# Load IDF
# --------------------------------------------------
idf = IDF(str(BASE_IDF), str(EPW_PATH))

# --------------------------------------------------
# Modify Lighting
# --------------------------------------------------
for lights in idf.idfobjects["LIGHTS"]:
    if lights.Design_Level_Calculation_Method == "Watts/Area":
        lights.Watts_per_Floor_Area = float(lights.Watts_per_Floor_Area) * float(params["lighting_multiplier"])
    elif lights.Design_Level_Calculation_Method == "LightingLevel":
        lights.Lighting_Level = float(lights.Lighting_Level) * float(params["lighting_multiplier"])
    elif lights.Design_Level_Calculation_Method == "Watts/Person":
        lights.Watts_per_Person = float(lights.Watts_per_Person) * float(params["lighting_multiplier"])

# --------------------------------------------------
# Modify Equipment
# --------------------------------------------------
for equip in idf.idfobjects["ELECTRICEQUIPMENT"]:
    if equip.Design_Level_Calculation_Method == "EquipmentLevel":
        equip.Design_Level = float(equip.Design_Level) * float(params["equipment_multiplier"])
    elif equip.Design_Level_Calculation_Method == "Watts/Area":
        equip.Watts_per_Floor_Area = float(equip.Watts_per_Floor_Area) * float(params["equipment_multiplier"])
    elif equip.Design_Level_Calculation_Method == "Watts/Person":
        equip.Watts_per_Person = float(equip.Watts_per_Person) * float(params["equipment_multiplier"])

# --------------------------------------------------
# Modify Cooling Coil COP
# --------------------------------------------------
for coil in idf.idfobjects["COIL:COOLING:DX:SINGLESPEED"]:
    coil.Gross_Rated_Cooling_COP = float(params["cop"])

# --------------------------------------------------
# Cooling Capacity & Safe Airflow Calculations 
# --------------------------------------------------
safe_flows = []

# 1. Update the Cooling Coils
for coil in idf.idfobjects["COIL:COOLING:DX:SINGLESPEED"]:
    val = coil.Gross_Rated_Total_Cooling_Capacity
    if val and val != "Autosize":
        # Set the new capacity based on the optimizer
        new_capacity = float(val) * float(params["cooling_capacity_multiplier"])
        coil.Gross_Rated_Total_Cooling_Capacity = new_capacity
        
        # Calculate a perfectly safe airflow (0.00005 m3/s per Watt)
        safe_flow = new_capacity * 0.00005
        coil.Rated_Air_Flow_Rate = safe_flow
        safe_flows.append(safe_flow)

# 2. Update the Fans to match the safe airflow
for i, fan in enumerate(idf.idfobjects["FAN:ONOFF"]):
    if i < len(safe_flows):
        # Apply the fan multiplier to the safe base flow
        fan.Maximum_Flow_Rate = safe_flows[i] * float(params["fan_flow_multiplier"])

# 3. Update the PTAC units to allow this much air through the vents
for i, ptac in enumerate(idf.idfobjects["ZONEHVAC:PACKAGEDTERMINALAIRCONDITIONER"]):
    if i < len(safe_flows):
        final_flow = safe_flows[i] * float(params["fan_flow_multiplier"])
        ptac.Cooling_Supply_Air_Flow_Rate = final_flow
        ptac.Heating_Supply_Air_Flow_Rate = final_flow
        ptac.No_Load_Supply_Air_Flow_Rate = final_flow

# --------------------------------------------------
# Inject measured occupancy schedule
# --------------------------------------------------
idf.newidfobject(
    "SCHEDULE:FILE",
    Name="Measured_Occupancy",
    Schedule_Type_Limits_Name="Fraction",
    File_Name=str(occ_file.resolve()),
    Column_Number=1,
    Rows_to_Skip_at_Top=0,
    Number_of_Hours_of_Data=8760,
    Column_Separator="Comma",
    Minutes_per_Item=5
)

for people in idf.idfobjects["PEOPLE"]:
    people.Number_of_People_Schedule_Name = "Measured_Occupancy"
    if people.Number_of_People:
        people.Number_of_People *= float(params["occupancy_multiplier"])
    elif people.People_per_Floor_Area:
        people.People_per_Floor_Area *= float(params["occupancy_multiplier"])

# --------------------------------------------------
# Inject measured thermostat schedule
# --------------------------------------------------
idf.newidfobject(
    "SCHEDULE:FILE",
    Name="Measured_Setpoint",
    Schedule_Type_Limits_Name="Temperature",
    File_Name=str(set_file.resolve()),
    Column_Number=1,
    Rows_to_Skip_at_Top=0,
    Number_of_Hours_of_Data=8760,
    Column_Separator="Comma",
    Minutes_per_Item=5
)

for thermo in idf.idfobjects["THERMOSTATSETPOINT:SINGLECOOLING"]:
    thermo.Setpoint_Temperature_Schedule_Name = "Measured_Setpoint"

# --------------------------------------------------
# Inject measured workshop temp schedule & Auto-Repair Surfaces
# --------------------------------------------------
idf.newidfobject(
    "SCHEDULE:FILE",
    Name="Workshop_Temp",
    Schedule_Type_Limits_Name="Temperature",
    File_Name=str(wrk_file.resolve()),
    Column_Number=1,
    Rows_to_Skip_at_Top=0,
    Number_of_Hours_of_Data=8760,
    Column_Separator="Comma",
    Minutes_per_Item=5
)

# 1. Create the Workshop OSC (Fluctuating sensor data)
workshop_osc = idf.newidfobject("SURFACEPROPERTY:OTHERSIDECOEFFICIENTS")
workshop_osc.obj = [
    "SURFACEPROPERTY:OTHERSIDECOEFFICIENTS", "Workshop_OSC", 
    0, 32, 1, 0, 0, 0, 0, "Workshop_Temp" 
]

# 2. Create the Cooled Room OSC (Constant AC Temperature)
cooled_osc = idf.newidfobject("SURFACEPROPERTY:OTHERSIDECOEFFICIENTS")
cooled_osc.obj = [
    "SURFACEPROPERTY:OTHERSIDECOEFFICIENTS", "CooledRoom_OSC", 
    0, 32, 1, 0, 0, 0, 0 
]

# 3. Repair and route specific surfaces
AC_WALL_NAME = "Surface 49" # Verify this matches your base model!

for surface in idf.idfobjects["BUILDINGSURFACE:DETAILED"]:
    # Lock out the sun and wind for everything
    surface.Outside_Boundary_Condition = "OtherSideCoefficients"
    surface.Sun_Exposure = "NoSun"
    surface.Wind_Exposure = "NoWind"
    
    # Route the Floor and the specific AC Wall to the Cooled Room
    if surface.Surface_Type.upper() == "FLOOR":
        surface.Outside_Boundary_Condition_Object = "CooledRoom_OSC"
    elif surface.Name.upper() == AC_WALL_NAME.upper():
        surface.Outside_Boundary_Condition_Object = "CooledRoom_OSC"
    else:
        # Route everything else to the Hot Workshop
        surface.Outside_Boundary_Condition_Object = "Workshop_OSC"

# --------------------------------------------------
# Make AC units always available 
# --------------------------------------------------
for ptac in idf.idfobjects["ZONEHVAC:PACKAGEDTERMINALAIRCONDITIONER"]:
    ptac.Availability_Schedule_Name = "Always On Discrete"

for fan in idf.idfobjects["FAN:ONOFF"]:
    fan.Availability_Schedule_Name = "Always On Discrete"

# --------------------------------------------------
# Save modified IDF and Run
# --------------------------------------------------
idf_path = RUN_DIR / "model.idf"
idf.saveas(str(idf_path))

cmd = [
    ENERGYPLUS_EXE,
    "-w", str(EPW_PATH),
    "-d", str(RUN_DIR),
    str(idf_path)
]

print("Launching EnergyPlus simulation...")
try:
    subprocess.run(cmd, check=True, capture_output=True, text=True)
except subprocess.CalledProcessError as e:
    print("EnergyPlus simulation failed!")
    print(e.stderr) 
    raise
    
print("Simulation finished.")
