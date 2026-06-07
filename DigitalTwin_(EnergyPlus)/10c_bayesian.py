from pathlib import Path
import subprocess
import pandas as pd
import sys
import shutil
from skopt import gp_minimize
from skopt.utils import use_named_args
from script6b_para_bounds import PARAM_SPACE

PROJECT_DIR = Path(__file__).resolve().parents[1]

RUN_SCRIPT = PROJECT_DIR / "Scripts" / "11c_run_sim.py"
SCORE_SCRIPT = PROJECT_DIR / "Scripts" / "12b_scoring.py"

RESULT_FILE = PROJECT_DIR / "results" / "current_run" / "current_run_score.csv"
PARAM_FILE = PROJECT_DIR / "calibration" / "current_params.csv"

HISTORY_FILE = PROJECT_DIR / "results" / "bayesian_scoring.csv"

CALIBRATED_DIR = PROJECT_DIR / "results" / "calibrated_model"
CALIBRATED_DIR.mkdir(parents=True, exist_ok=True)

CURRENT_SESSION_BEST_FILE = PROJECT_DIR / "results" / "current_session_best.csv"

iteration = 0
best_cvrmse = float("inf")            # tracks lowest CVRMSE Overall
best_params = None

best_valid_cvrmse = float("inf")      # tracks lowest CVRMSE that PASSES ASHRAE
best_valid_params = None

@use_named_args(PARAM_SPACE)
def objective(**params):

    global iteration, best_cvrmse, best_params, best_valid_cvrmse, best_valid_params
    iteration += 1

    print("\n====================================")
    print(f"Bayesian Iteration {iteration}")
    print("====================================")

    # Save parameters for simulation (params is packed as a dictionary)
    pd.DataFrame([params]).to_csv(PARAM_FILE, index=False)

    # Run EnergyPlus simulation
    subprocess.run([sys.executable, str(RUN_SCRIPT)], check=True)

    # Run scoring script
    subprocess.run([sys.executable, str(SCORE_SCRIPT)], check=True)

    # Read scoring results
    df = pd.read_csv(RESULT_FILE)

    temp_cvrmse = df.iloc[0]["temp_cvrmse"]
    temp_nmbe   = df.iloc[0]["temp_nmbe"]

    rh_cvrmse   = df.iloc[0]["rh_cvrmse"]
    rh_nmbe     = df.iloc[0]["rh_nmbe"]

    ac_cvrmse   = df.iloc[0]["ac_cvrmse"]
    ac_nmbe     = df.iloc[0]["ac_nmbe"]

    score = df.iloc[0]["score"]

    print("\nCalibration metrics")
    print("----------------------")
    print(f"AC CVRMSE  : {ac_cvrmse:.2f}")
    print(f"AC NMBE    : {ac_nmbe:.2f}")


    row = {
        "iteration": iteration,
        **params,
        "temp_cvrmse": temp_cvrmse,
        "temp_nmbe": temp_nmbe,
        "rh_cvrmse": rh_cvrmse,
        "rh_nmbe": rh_nmbe,
        "ac_cvrmse": ac_cvrmse,
        "ac_nmbe": ac_nmbe,
        "score": score
    }

    # append to history file
    if HISTORY_FILE.exists():
        hist = pd.read_csv(HISTORY_FILE)
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])

    hist.to_csv(HISTORY_FILE, index=False)

    # ------------------------------------------------
    # Save Top 10
    # ------------------------------------------------
    TOP10_FILE = PROJECT_DIR / "results" / "top10_new_models.csv"
    top10 = hist.sort_values("ac_cvrmse").head(10)
    top10.to_csv(TOP10_FILE, index=False)

    # ------------------------------------------------
    # Track best model & Save the IDF
    # ------------------------------------------------
    ashrae_pass = (ac_cvrmse <= 29.97 and abs(ac_nmbe) <= 10)

    # Always track lowest CVRMSE for THIS session (even if not ASHRAE valid)
    if ac_cvrmse < best_cvrmse:
        best_cvrmse = ac_cvrmse
        best_params = params
        
        print("\n*** CURRENT LOWEST AC CVRMSE FOR THIS RUN ***")
        print(f"AC CVRMSE: {best_cvrmse:.2f} (Found at Iteration {iteration})")
        
        # ---> ADDED: Save the best row of this session to CSV (overwrites previous)
        pd.DataFrame([row]).to_csv(CURRENT_SESSION_BEST_FILE, index=False)

    if ashrae_pass and ac_cvrmse < best_valid_cvrmse:
        best_valid_cvrmse = ac_cvrmse
        best_valid_params = params

        print("\n🎯 *** NEW BEST ASHRAE-COMPLIANT MODEL FOUND ***")
        print(f"AC CVRMSE: {best_valid_cvrmse:.2f}")
        print(f"AC NMBE: {ac_nmbe:.2f}")
        print("Parameters:", best_valid_params)

        pd.DataFrame([best_valid_params]).to_csv(
            PROJECT_DIR / "results" / "best_parameters.csv",
            index=False
        )

        pd.DataFrame([best_valid_params]).to_csv(
            CALIBRATED_DIR / "calibrated_parameters.csv",
            index=False
        )

        # Safely copy the IDF to the calibrated folder now that it's the new best
        src_idf = PROJECT_DIR / "results" / "current_run" / "model.idf"
        dst_idf = CALIBRATED_DIR / "calibrated_model.idf"
        shutil.copy(src_idf, dst_idf)

    # ------------------------------------------------
    # ✅ NEW EARLY STOP (ASHRAE-BASED)
    # ------------------------------------------------
    if ashrae_pass:
        print("\n🎯 ASHRAE CALIBRATION ACHIEVED!")
        print(f"AC CVRMSE: {ac_cvrmse:.2f}")
        print(f"AC NMBE: {ac_nmbe:.2f}")
        print("Stopping optimization early. Your files are ready in the 'calibrated_model' folder.")
        sys.exit(0) 

    # ✅ ALWAYS RETURN SCORE
    return float(score)

if __name__ == "__main__":

    result = gp_minimize(
        func=objective,
        dimensions=PARAM_SPACE,
        n_calls=40,
        n_initial_points=8,
        random_state=42
    )

    print("\n====================================")
    print("Optimization finished")
    print("====================================")