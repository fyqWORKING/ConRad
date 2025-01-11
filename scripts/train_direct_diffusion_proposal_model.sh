python $NUPLAN_DEVKIT_ROOT/nuplan/planning/script/run_training.py \
    experiment_name=train_diffusion_proposal_model \
    job_name=train_DE_PDM_mini_singapore \
    py_func=train \
    +training=training_diffusion_proposal_model \
    lightning.trainer.params.max_epochs=10 \
    data_loader.params.batch_size=16 \
    data_loader.val_params.batch_size=4 \
    data_loader.val_params.shuffle=True \
    data_loader.params.num_workers=8 \
    data_loader.params.pin_memory=False \
    lightning.trainer.params.limit_val_batches=10 \
    lightning.trainer.params.check_val_every_n_epoch=1 \
    callbacks.model_checkpoint_callback.every_n_epochs=1 \
    optimizer=adamw \
    optimizer.lr=1e-4 \
    callbacks.visualization_callback.skip_train=False \
    model.T=100