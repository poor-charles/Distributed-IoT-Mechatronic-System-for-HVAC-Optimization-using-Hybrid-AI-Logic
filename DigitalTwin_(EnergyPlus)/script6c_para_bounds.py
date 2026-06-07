from pathlib import Path
import pandas as pd
from skopt.space import Real

PROJECT_DIR = Path(__file__).resolve().parents[1]
CAL_DIR = PROJECT_DIR / "calibration"
CAL_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------
# Parameter space for Bayesian optimization
# ------------------------------------------------
PARAM_SPACE = [
    # 1. Lighting Multiplier
    Real(0.87, 0.88, name="lighting_multiplier"),
    
    # 2. Equipment Multiplier
    Real(0.79, 0.8, name="equipment_multiplier"), 
    
    # 3. AC Efficiency (COP)
    Real(2.7, 2.71, name="cop"),
    
    # 4. Occupancy Multiplier
    Real(0.999, 1.0, name="occupancy_multiplier"), 
    
    # 5. Cooling Capacity Multiplier
    Real(1.406, 1.407, name="cooling_capacity_multiplier"),

    # 6. Fan Flow Multiplier
    Real(0.55, 0.56, name="fan_flow_multiplier"),
    
]

# ------------------------------------------------
# Convert bounds to DataFrame
# ------------------------------------------------
if __name__ == "__main__":
    bounds = pd.DataFrame({
        "parameter": [p.name for p in PARAM_SPACE],
        "lower_bound": [p.low for p in PARAM_SPACE],
        "upper_bound": [p.high for p in PARAM_SPACE]
    })

    out_path = CAL_DIR / "parameter_bounds.csv"
    bounds.to_csv(out_path, index=False)

    print("Parameter bounds file created successfully.\n")
    print(bounds.to_string(index=False))
    print(f"\nSaved to:\n{out_path}")