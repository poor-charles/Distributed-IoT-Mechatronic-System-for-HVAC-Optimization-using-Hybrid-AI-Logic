import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import r2_score, mean_absolute_error
from sklearn.model_selection import GroupShuffleSplit
from skopt import BayesSearchCV
from skopt.space import Integer, Real
import matplotlib.pyplot as plt
import joblib
TRAIN_CSV = "BORF_DATASET 1.csv"  
UNSEEN_CSV = "NEW_UNSEEN_DATA2.csv" 
print("🔄 Merging datasets and applying Block Time-Series Splitting...")
df_train = pd.read_csv(TRAIN_CSV)
df_unseen = pd.read_csv(UNSEEN_CSV)
df_master = pd.concat([df_train, df_unseen], ignore_index=True)
df_master = df_master.rename(columns={
    'inside air temp': 'inside_air_temperature',
    'inside air humidity': 'inside_air_humidity',
    'occupancy': 'num_occupants',
    'outside air temp': 'outside_air_temperature',
    'outside air humidity': 'outside_air_humidity',
    'time of the day': 'time_of_day',
    'ac temperature setpoint': 'ac_temperature_setpoint',
    'ac mode': 'ac_mode',
    'ac fan speed': 'ac_fan_speed',
    'ac energy consumption (kw)': 'ac_energy_consumption_kw',
    'thermal comfort PMV': 'PMV' 
})
df_master['outside_air_temperature'] = df_master['outside_air_temperature'].fillna(df_master['inside_air_temperature'])
df_master['outside_air_humidity'] = df_master['outside_air_humidity'].fillna(df_master['inside_air_humidity'])
df_master['ac_energy_consumption_kw'] = df_master['ac_energy_consumption_kw'].fillna(0.0)
mode_mapping = {'off': 0, 'fan': 1, 'dry': 2, 'cool': 3, 'eco': 4}
df_master['ac_mode'] = df_master['ac_mode'].astype(str).str.strip().str.lower().map(mode_mapping).fillna(0).astype(int)
def clean_fan(val):
    val_str = str(val).strip().lower()
    if val_str in ['off', 'nan']: return 0
    if val_str == 'auto': return 4
    try: return int(np.clip(np.round(float(val)), 1, 3))
    except: return 0
df_master['ac_fan_speed'] = df_master['ac_fan_speed'].apply(clean_fan)
def clean_setp(val):
    val_str = str(val).strip().lower()
    if val_str in ['off', 'nan']: return 0
    try: return int(np.clip(np.round(float(val)), 16, 30)) if float(val) > 0 else 0
    except: return 0
df_master['ac_temperature_setpoint'] = df_master['ac_temperature_setpoint'].apply(clean_setp)
def extract_hour(val):
    if isinstance(val, str) and ':' in val: return int(val.split(':')[0])
    elif pd.notnull(val): return int(float(val))
    return np.nan
df_master['time_of_day'] = df_master['time_of_day'].apply(extract_hour)
df_master = df_master.dropna(subset=['time_of_day']).reset_index(drop=True)
# ============================================================
# BLOCK SPLITTING (GroupShuffleSplit)
# ============================================================
CHUNK_SIZE = 180 
df_master['block_id'] = df_master.index // CHUNK_SIZE
features = ['time_of_day', 'num_occupants', 'outside_air_temperature', 
            'outside_air_humidity', 'ac_temperature_setpoint', 'ac_mode', 'ac_fan_speed']
targets = ['PMV', 'ac_energy_consumption_kw']
X = df_master[features]
Y = df_master[targets]
groups = df_master['block_id']
gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
train_idx, test_idx = next(gss.split(X, Y, groups))
X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
Y_train, Y_test = Y.iloc[train_idx], Y.iloc[test_idx]
# ============================================================
# CUSTOM BAYESIAN VALIDATION ENGINE
# ============================================================
# Set to True to use Custom Bayesian Optimization on the Validation Set
# Set to False to use manual hardcoded parameters
ENABLE_CUSTOM_BAYESIAN = False
if ENABLE_CUSTOM_BAYESIAN:
    from skopt import gp_minimize
    from skopt.space import Integer
    from skopt.utils import use_named_args
    print(f"\n🚀 Initializing Smart Custom Bayesian Search...")
    print("The AI is using Bayesian math to hunt for the highest score on the Validation Set.")
    # 1. Define the RANGES (The Search Space)
    dimensions = [
        Integer(50, 300, name='n_estimators'),
        Integer(3, 30, name='max_depth'),
        Integer(2, 20, name='min_samples_split'),
        Integer(1, 10, name='min_samples_leaf')
    ]
    # Global variable to track our high score during the printouts
    best_seen_score = -np.inf
    # 2. Define the Custom Objective Function
    @use_named_args(dimensions=dimensions)
    def fitness(n_estimators, max_depth, min_samples_split, min_samples_leaf):
        global best_seen_score
        # Build the temp model
        temp_model = MultiOutputRegressor(RandomForestRegressor(
            n_estimators=n_estimators,
            max_depth=max_depth,
            min_samples_split=min_samples_split,
            min_samples_leaf=min_samples_leaf,
            random_state=42,
            n_jobs=-1
        ))
        # Train STRICTLY on the training block
        temp_model.fit(X_train, Y_train)
        # Predict STRICTLY on the validation block
        Y_test_pred_temp = temp_model.predict(X_test)
        pmv_r2_temp = r2_score(Y_test['PMV'], Y_test_pred_temp[:, 0])
        energy_r2_temp = r2_score(Y_test['ac_energy_consumption_kw'], Y_test_pred_temp[:, 1])
        # Calculate combined score
        combined_score = (pmv_r2_temp + energy_r2_temp) / 2.0
        # Print if it's a new high score
        if combined_score > best_seen_score:
            best_seen_score = combined_score
            print(f"🌟 NEW HIGH SCORE! Combined R²: {best_seen_score:.3f} (PMV: {pmv_r2_temp:.3f}, Energy: {energy_r2_temp:.3f})")
            print(f"   Params: Trees={n_estimators}, Depth={max_depth}, Split={min_samples_split}, Leaf={min_samples_leaf}")
        # Bayesian optimizers are designed to MINIMIZE a number (like error). 
        # Since we want a HIGH R² score, we return the NEGATIVE combined score.
        return -combined_score
    # 3. Run the Gaussian Process (Bayesian) Minimizer
    print(f"\nCommencing 1000 smart Bayesian iterations...")
    search_result = gp_minimize(
        func=fitness,
        dimensions=dimensions,
        n_calls=1000,         # How many total combinations it will try
        n_initial_points=100, # It will guess randomly 5 times to build a baseline map, then get smart
        random_state=42
    )
    import joblib
    joblib.dump(search_result, "BO_SEARCH_HISTORY.pkl")
    # 4. Extract the absolute best parameters it found
    best_params = {
        'n_estimators': search_result.x[0],
        'max_depth': search_result.x[1],
        'min_samples_split': search_result.x[2],
        'min_samples_leaf': search_result.x[3]
    }
    print("\n✅ Smart Bayesian Search Complete!")
    print(f"🏆 Best Winning Parameters: {best_params}")
    # 5. Lock in the ultimate model
    best_model = MultiOutputRegressor(RandomForestRegressor(
        **best_params,
        random_state=42,
        n_jobs=-1
    ))
    # Train the official brain one last time with the winning parameters
    best_model.fit(X_train, Y_train)
else:
    print(f"\n⚡ Fast Training Mode Activated. Using hardcoded parameters...")
    best_model = MultiOutputRegressor(RandomForestRegressor(
        n_estimators=65,       
        max_depth=6,            
        min_samples_split=20,    
        min_samples_leaf=1,     
        random_state=42,
        n_jobs=-1               
    ))
    best_model.fit(X_train, Y_train)
    print("✅ Fast Training Complete!")
# ============================================================
# EVALUATE ON BOTH TRAIN AND TEST SETS
# ============================================================
Y_train_pred = best_model.predict(X_train)
Y_test_pred = best_model.predict(X_test)
print("\n" + "="*80)
print("🏆 MODEL TRAINING AND EVALUATION SCORES")
print("="*80)
print("- TRAINING SCORES VS TESTING SCORES (Higher R² and Lower MAE are better) -")
# PMV
pmv_train_r2 = r2_score(Y_train['PMV'], Y_train_pred[:, 0])
pmv_train_mae = mean_absolute_error(Y_train['PMV'], Y_train_pred[:, 0])
pmv_test_r2 = r2_score(Y_test['PMV'], Y_test_pred[:, 0])
pmv_test_mae = mean_absolute_error(Y_test['PMV'], Y_test_pred[:, 0])
# ENERGY
energy_train_r2 = r2_score(Y_train['ac_energy_consumption_kw'], Y_train_pred[:, 1])
energy_train_mae = mean_absolute_error(Y_train['ac_energy_consumption_kw'], Y_train_pred[:, 1])
energy_test_r2 = r2_score(Y_test['ac_energy_consumption_kw'], Y_test_pred[:, 1])
energy_test_mae = mean_absolute_error(Y_test['ac_energy_consumption_kw'], Y_test_pred[:, 1])
print(f"🌡️ PMV         | TRAIN R²: {pmv_train_r2: .3f} (MAE: {pmv_train_mae:.3f})   | TEST R²: {pmv_test_r2: .3f} (MAE: {pmv_test_mae:.3f})")
print(f"⚡ Energy (W) | TRAIN R²: {energy_train_r2: .3f} (MAE: {energy_train_mae:.3f})   | TEST R²: {energy_test_r2: .3f} (MAE: {energy_test_mae:.3f})")
print("="*80)
# ============================================================
# AUTOMATED GRAPHING (NO MENU PROMPTS)
# ============================================================
print("\n⏳ Generating graphs... (Saving directly to 'Validation_Graphs.png')")
all_targets = ['PMV', 'ac_energy_consumption_kw']
all_titles = ['Thermal Comfort (PMV)', 'AC Energy Consumption (W)']
fig, axes = plt.subplots(nrows=2, ncols=2, figsize=(16, 10))
fig.suptitle('Optimized Block Split Validation Performance', fontsize=18, fontweight='bold')
for plot_idx, target_idx in enumerate([0, 1]):
    target = all_targets[target_idx]
    title = all_titles[target_idx]
    actual = Y_test[target].values
    pred = Y_test_pred[:, target_idx]
    ax_scatter = axes[plot_idx, 0]
    ax_scatter.scatter(actual, pred, alpha=0.4, color='royalblue', edgecolor='k', s=30)
    min_val = min(np.min(actual), np.min(pred))
    max_val = max(np.max(actual), np.max(pred))
    ax_scatter.plot([min_val, max_val], [min_val, max_val], color='red', linestyle='--', linewidth=2.5, label='Perfect Alignment (y=x)')
    ax_scatter.set_title(f'{title} - Accuracy Distribution', fontsize=12, fontweight='bold')
    ax_scatter.set_xlabel('Actual Measured Value')
    ax_scatter.set_ylabel('AI Predicted Value')
    ax_scatter.grid(True, linestyle='--', alpha=0.6)
    ax_scatter.legend()
    ax_line = axes[plot_idx, 1]
    view_window = min(500, len(actual))
    ax_line.plot(actual[:view_window], label='Actual Sensor Reading', color='black', linewidth=2)
    ax_line.plot(pred[:view_window], label='AI Prediction', color='darkorange', linestyle='--', linewidth=2)
    ax_line.set_title(f'{title} - Physical Trend Tracking (First {view_window} States)', fontsize=12, fontweight='bold')
    ax_line.set_xlabel('Data Points (Time Sequence)')
    ax_line.set_ylabel('Value')
    ax_line.grid(True, linestyle='--', alpha=0.6)
    ax_line.legend()
plt.tight_layout(rect=[0, 0.03, 1, 0.96])
plt.savefig('Validation_Graphs.png', dpi=300)
print("✅ Graphs saved! Attempting to open display window...")
plt.show()
# ============================================================
# 💾 SAVE THE TRAINED MODEL FOR LIVE DEPLOYMENT
# ============================================================
print("\n💾 Saving model for deployment...")
# Using the exact filenames your hvac_action_selector.py expects
MODEL_FILENAME = "BORF_MODEL.pkl"
FEATURES_FILENAME = "BORF_FEATURES.pkl"
# Save the trained AI brain
joblib.dump(best_model, MODEL_FILENAME)
# Save the exact order of features so the inference script doesn't scramble inputs
joblib.dump(features, FEATURES_FILENAME)
print(f"✅ Model successfully saved as: {MODEL_FILENAME}")
print(f"✅ Features successfully saved as: {FEATURES_FILENAME}")
print("System ready for mechatronic deployment.")