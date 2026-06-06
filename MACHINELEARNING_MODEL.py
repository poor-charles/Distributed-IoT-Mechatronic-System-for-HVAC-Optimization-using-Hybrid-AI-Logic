# hvac_action_selector.py
# =============================================================
# Uses the trained BORF model
# METHOD: PURE GRID SEARCH (Deterministic - No Randomness)
# PRIORITIES: Comfort is MANDATORY. Energy is secondary.
# =============================================================

import pandas as pd
import numpy as np
import joblib                                  
import itertools

# -----------------------------
# USER-CONFIG
# -----------------------------
DATA_CSV = "BORF_DATASET 1.csv"                  
MODEL_PKL = "BORF_MODEL.pkl"              
FEATURES_PKL = "BORF_FEATURES.pkl"             
SETPOINT_MIN = 17 
SETPOINT_MAX = 28                              
TOP_K = 5                                      

# WEIGHTS
ALPHA_ENERGY = 0.5 
COMFORT_PENALTY_MULTIPLIER = 2.0 

# -----------------------------
# 1) Load trained model + feature order
# -----------------------------
model = joblib.load(MODEL_PKL)
feature_order = joblib.load(FEATURES_PKL)

# -----------------------------
# 2) Explicit Category Mapping (ADDED ECO MODE)
# -----------------------------
ac_mode_map = {'off': 0, 'fan': 1, 'dry': 2, 'cool': 3, 'eco': 4}

# -----------------------------
# 3) Define candidate actions
# -----------------------------
request_modes = ['off', 'fan', 'cool', 'dry', 'eco']               
mode_candidates = [(m, ac_mode_map[m]) for m in request_modes]
fan_candidates = [('off', 0), ('1', 1), ('2', 2), ('3', 3), ('auto', 4)]   

# -----------------------------
# 4) Main Solver Function
# -----------------------------

# =============================================================
# 🧠 MODULAR EXPERT RULE ENGINE
# =============================================================
# Negative numbers = REWARD (AI prefers it)
# Positive numbers = PENALTY (AI avoids it)

def rule_empty_room_shutoff(env, action, pmv_pred, energy_pred, current_obj):
    """If occupancy is 0, the ONLY logical action is OFF."""
    if env['num_occupants'] == 0:
        if action['mode_label'] == 'off':
            return current_obj - 500.0  
        else:
            return current_obj + 500.0  
    return current_obj

def rule_crowd_fast_cooling(env, action, pmv_pred, energy_pred, current_obj):
    """If occupancy is high (>10), reward high fan speeds for faster heat removal."""
    if env['num_occupants'] > 10:
        if action['fan_label'] in ['3', 'auto']:
            return current_obj - 3.0  
        elif action['fan_label'] == '1':
            return current_obj + 3.0  
    return current_obj

def rule_crowded_room_physics(env, action, pmv_pred, energy_pred, current_obj):
    """15 people = ~1500W of heat. 28C Dry will literally bake them."""
    if env['num_occupants'] >= 7 and env['outside_air_temperature'] >= 29.0:
        # Ban DRY and ECO modes (won't push enough cold air for a heavy crowd)
        if action['mode_label'] in ['dry', 'eco']:
            return current_obj + 200.0  
        # Ban weak setpoints 
        if action['setpoint'] != 'N/A' and int(action['setpoint']) >= 26:
            return current_obj + 200.0
    return current_obj

# --- ACTIVE RULE LIST ---
ACTIVE_RULES = [
    rule_empty_room_shutoff,
    rule_crowd_fast_cooling,
    rule_crowded_room_physics
]

# =============================================================

def find_best_action_for_environment(env):
    df_train = pd.read_csv(DATA_CSV)
    # Changed from max watts to max accumulated kWh for the scaling factor
    energy_scale = df_train['ac_energy_consumption_kw'].max() if 'ac_energy_consumption_kw' in df_train.columns else 1.5 

    # Create every possible combination of settings
    combinations = list(itertools.product(
        [0] + list(range(SETPOINT_MIN, SETPOINT_MAX + 1)), 
        mode_candidates, 
        fan_candidates
    ))

    valid_rows = []
    
    # 1. Filter valid combinations first
    for setp, (mode_lab, mode_code), (fan_lab, fan_code) in combinations:
        # Enforce physical logic
        if mode_lab == 'off':
            if setp != 0 or fan_code != 0: continue
        elif mode_lab == 'fan':
            if setp != 0 or fan_code == 0 or fan_code == 4: continue
        elif mode_lab == 'dry':
            if setp == 0 or fan_code != 4: continue
        elif mode_lab in ['cool', 'eco']: # Treat eco physically similar to cool
            if setp == 0 or fan_code == 0 or fan_code == 4: continue
        
        valid_rows.append({
            'setpoint': setp,
            'mode_label': mode_lab,
            'mode_code': mode_code,
            'fan_label': fan_lab,
            'fan_code': fan_code
        })

    # 2. Batch Predict (UPDATED FEATURES TO MATCH NEW MODEL)
    X_batch = pd.DataFrame([
        {
            'time_of_day': env['time_of_day'], # <-- NEW INPUT
            'num_occupants': env['num_occupants'],
            'outside_air_temperature': env['outside_air_temperature'],
            'outside_air_humidity': env['outside_air_humidity'],
            'ac_temperature_setpoint': r['setpoint'],
            'ac_mode': r['mode_code'],
            'ac_fan_speed': r['fan_code']
            # delta_t and human heat load have been removed!
        } for r in valid_rows
    ])[feature_order]

    predictions = model.predict(X_batch)
    
    # 3. Calculate Objectives
    results = []
    for i, r in enumerate(valid_rows):
        pmv_pred = float(predictions[i][0])
        energy_pred = float(predictions[i][1]) # This is now in Accumulated kWh
        temp_pred = float(predictions[i][2])

        # --- BASE ASHRAE LOGIC ---
        if -0.1 <= pmv_pred <= 0.4:
            pmv_penalty = 0.0 # Perfect!
        else:
            ideal_zone = 0.15
            dist = abs(pmv_pred - ideal_zone)
            pmv_penalty = 10.0 + (dist * COMFORT_PENALTY_MULTIPLIER) 

        # Calculate base objective
        obj = pmv_penalty + ALPHA_ENERGY * (energy_pred / energy_scale)

        # --- 🧠 APPLY MODULAR EXPERT RULES ---
        for rule_function in ACTIVE_RULES:
            obj = rule_function(env, r, pmv_pred, energy_pred, obj)

        display_setp = str(r['setpoint']) if r['setpoint'] != 0 else "N/A"

        results.append({
            'setpoint': display_setp,
            'mode': r['mode_label'],
            'fan': r['fan_label'],
            'pred_PMV': round(pmv_pred, 3),
            'pred_kWh': round(energy_pred, 3), # Changed to kWh
            'pred_Inside_Temp': round(temp_pred, 1),
            'objective': obj,
            '_setp_val': r['setpoint'],
            '_mode_code': r['mode_code'],
            '_fan_code': r['fan_code']
        })
    
    # 4. Sort and Pick Best
    grid_df = pd.DataFrame(results)
    grid_df_sorted = grid_df.sort_values(['objective', 'pred_kWh']).reset_index(drop=True)
    
    best_row = grid_df_sorted.iloc[0]

    best_action = {
        'ac_temperature_setpoint': best_row['_setp_val'],
        'ac_mode_label': best_row['mode'],
        'ac_mode_code': best_row['_mode_code'],
        'ac_fan_speed_label': best_row['fan'],
        'ac_fan_code': best_row['_fan_code']
    }

    return {
        'best_action': best_action, 
        'best_pmv': best_row['pred_PMV'], 
        'best_energy_kwh': best_row['pred_kWh'], 
        'best_temp': best_row['pred_Inside_Temp'], 
        'best_objective': best_row['objective'], 
        'grid_ranked': grid_df_sorted.drop(columns=['_setp_val', '_mode_code', '_fan_code'])
    }

# -----------------------------
# 5) Interactive Example Usage
# -----------------------------
if __name__ == "__main__":
    print("\n=== HVAC OPTIMIZER (DETERMINISTIC GRID SEARCH) ===")
    print("Enter current external environment values:")
    time_val = int(input("Time of the Day (0-23 hours): ") or 14) # <-- NEW INPUT
    occupants = int(input("Occupancy (people): ") or 4)
    outside_temp = float(input("Outside air temperature (°C): ") or 29.0)
    outside_hum = float(input("Outside air humidity (%): ") or 80.0)

    env_now = {
        'time_of_day': time_val,
        'num_occupants': occupants, 
        'outside_air_temperature': outside_temp,
        'outside_air_humidity': outside_hum
    }

    print("\n🔎 Calculating all possible actions...")
    result = find_best_action_for_environment(env_now)

    print("\n✅ BEST ACTION FOUND:")
    b = result['best_action']
    
    display_setpoint = f"{b['ac_temperature_setpoint']} °C" if b['ac_temperature_setpoint'] != 0 else "N/A"
    
    print(f"  Setpoint:  {display_setpoint}")
    print(f"  Mode:      {b['ac_mode_label'].upper()}")
    print(f"  Fan speed: {b['ac_fan_speed_label'].upper()}")
    print("\n📊 EXPECTED ROOM RESULTS (At Equilibrium):")
    print(f"  Resulting Inside Temp: {result['best_temp']:.1f} °C")
    print(f"  Resulting PMV:         {result['best_pmv']:.3f} (Comfortable: -0.5 to +0.5)")
    print(f"  Accumulated Energy:    {result['best_energy_kwh']:.3f} w")

    print(f"\n🏆 Top {TOP_K} alternatives (ranked by objective):")
    print(result['grid_ranked'].drop(columns=['objective']).head(TOP_K).to_string(index=False))

    result['grid_ranked'].to_csv("best_actions_ranked.csv", index=False)
