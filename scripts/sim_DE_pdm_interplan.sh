# EXPERIMENT=sim_interplan_DE_pdm
# worker=single_machine_thread_pool \
# worker.use_process_pool=True \
# worker=sequential \
# "/home/fyq/nuplan/exp/exp/kinematic/kinematic/2024.10.28.15.00.47/best_model/epoch\=0-step\=9199.ckpt" 
# /home/fyq/nuplan/exp/exp/train_DE_PDM/train_DE_PDM/2024.12.02.19.51.29/best_model/epoch\=0-step\=919.ckpt
# "/home/fyq/nuplan/exp/exp/train_DE_PDM/train_DE_PDM/2024.12.02.19.51.29/checkpoints/epoch\=49.ckpt"
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.16.11.18.29/best_model/epoch\=33-step\=31279.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.17.20.26.44/best_model/epoch\=9-step\=9199.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.17.20.42.21/best_model/epoch\=2-step\=2759.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.17.21.36.05/best_model/epoch\=2-step\=2759.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.17.21.57.36/best_model/epoch\=6-step\=6439.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.17.22.25.36/best_model/epoch\=9-step\=9199.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.18.10.37.21/best_model/epoch\=9-step\=9199.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/different_pva_encoder/2024.12.19.10.42.27/best_model/epoch\=9-step\=9199.ckpt
# /home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.20.15.06.18/best_model/epoch\=9-step\=9199.ckpt
EXPERIMENT=test
NUPLAN_EXP_ROOT=~/interplan_workspace/exp/
INTERPLAN_PLUGIN_ROOT=~/interplan_workspace/interplan-plugin

python $INTERPLAN_PLUGIN_ROOT/interplan/planning/script/run_simulation.py \
+simulation=default_interplan_benchmark \
planner=de_pdm_planner \
planner.de_pdm_planner.checkpoint_path="/home/fyq/nuplan/exp/exp/train_diffusion_proposal_model/train_DE_PDM/2024.12.20.15.06.18/best_model/epoch\=9-step\=9199.ckpt" \
planner.de_pdm_planner.dump_gifs_path="/home/fyq/nuplan/exp/viz_diffusion_es" \
scenario_filter=interplan10 \
experiment_name=$EXPERIMENT \
ego_controller=one_stage_controller \
worker=single_machine_thread_pool \
worker.use_process_pool=True \
hydra.searchpath="[\
pkg://interplan.planning.script.config.common,\
pkg://interplan.planning.script.config.simulation,\
pkg://interplan.planning.script.experiments,\
pkg://tuplan_garage.planning.script.config.common,\
pkg://tuplan_garage.planning.script.config.simulation,\
pkg://nuplan.planning.script.config.common,\
pkg://nuplan.planning.script.config.simulation,\
pkg://nuplan.planning.script.experiments\
]"
