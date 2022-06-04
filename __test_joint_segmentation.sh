#!/bin/bash
# set the required env vars
models=$1
gpu_ids=$2
perform_filtration_model=$3 #True/False for model
perform_filtration_ds=True #clean support
export SOURCE=all #tesla
export CUDA_VISIBLE_DEVICES=$gpu_ids

source __set_suffix.sh $perform_filtration_model 60 use_pretrained_backbone
source set_env.sh

# link dataset variant of choice, useful for tesla
export TESLA_DATASET_VARIANT=$4
eval_episodes=1

RECORDS="$ROOT_DIR/records-non-oversampled"

cd $RECORDS; rm tesla; ln -s $TESLA_DATASET_VARIANT tesla; cd $ROOT_DIR;

ls -l $RECORDS # useful to check if sym links are correct

image_height=126

for MODEL in $models
do
  export EXP_GIN=${MODEL}_${SOURCE}
  if [ "$MODEL" == "baselinefinetune" ];
  then
    image_height=84
    backbone="resnet"
  else
    backbone="resnet34"
  fi

  export EXPNAME=${MODEL}_${SOURCE}${chkpt_suffix}${pretrained_phrase}-${backbone}

  for DATASET in tesla 
  do
    echo "MODEL-FILTER: $perform_filtration_model"
    echo "DATASET-FILTER: $perform_filtration_ds"
    echo "ROOT_DIR: $ROOT_DIR"
    BESTNUM=$5
    python -m meta_dataset.train \
      --is_training=False \
      --records_root_dir=$RECORDS \
      --summary_dir=${EXPROOT}/summaries/${EXPNAME} \
      --gin_config=meta_dataset/learn/gin/best/${EXP_GIN}.gin \
      --gin_bindings="Trainer.experiment_name='${EXPNAME}'" \
      --gin_bindings="Trainer.checkpoint_to_restore='${EXPROOT}/checkpoints/${EXPNAME}/model_${BESTNUM}.ckpt'" \
      --gin_bindings="Trainer.perform_filtration=${perform_filtration_ds}" \
      --gin_bindings="Learner.embedding_fn = @${backbone}" \
      --gin_bindings="DataConfig.image_height=${image_height}" \
      --gin_bindings="Trainer.num_eval_episodes=$eval_episodes" \
      --gin_bindings="Trainer.test_entire_test_set_using_single_episode=True" \
      --gin_bindings="benchmark.eval_datasets='$DATASET'"      
  done
done
