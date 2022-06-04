#!/bin/bash
source __select_best_model.sh $1 $2 $3 True $4 $6 $7

# link dataset variant of choice, useful for tesla
export TESLA_DATASET_VARIANT=$5

eval_episodes=1
backbone=$7
_backbone=$backbone
if test "$backbone" = "convnet"
then
    _backbone="four_layer_convnet"
fi

if test "$backbone" = "resnet34_ctx"
then
    _backbone="resnet34"
fi

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
  fi
  # export EXPNAME=${EXP_GIN}${nve_suffix}
  # for DATASET in omniglot 
  for DATASET in tesla 
  do
    echo "MODEL-FILTER: $perform_filtration_model"
    echo "DATASET-FILTER: $perform_filtration_ds"
    echo "ROOT_DIR: $ROOT_DIR"
    if test "$backbone" = "" # default backbone
    then
      # set BESTNUM to the "best_update_num" field in the corresponding best_....txt
      export BESTNUM=$(grep best_update_num ${EXPROOT}/best_$EXPNAME.txt | awk '{print $2;}')
      BESTNUM=$8
      python -m meta_dataset.train \
        --is_training=False \
        --records_root_dir=$RECORDS \
        --summary_dir=${EXPROOT}/summaries/${EXPNAME} \
        --gin_config=meta_dataset/learn/gin/best/${EXP_GIN}.gin \
        --gin_bindings="Trainer.experiment_name='${EXPNAME}'" \
        --gin_bindings="Trainer.checkpoint_to_restore='${EXPROOT}/checkpoints/${EXPNAME}/model_${BESTNUM}.ckpt'" \
        --gin_bindings="Trainer.perform_filtration=${perform_filtration_ds}" \
        --gin_bindings="DataConfig.image_height=${image_height}" \
        --gin_bindings="Trainer.num_eval_episodes=$eval_episodes" \
        --gin_bindings="Trainer.test_entire_test_set_using_single_episode=True" \
        --gin_bindings="benchmark.eval_datasets='$DATASET'"
    else
      export BESTNUM=$(grep best_update_num ${EXPROOT}/best_$EXPNAME.txt | awk '{print $2;}')
      BESTNUM=$8
      python -m meta_dataset.train \
        --is_training=False \
        --records_root_dir=$RECORDS \
        --summary_dir=${EXPROOT}/summaries/${EXPNAME} \
        --gin_config=meta_dataset/learn/gin/best/${EXP_GIN}.gin \
        --gin_bindings="Trainer.experiment_name='${EXPNAME}'" \
        --gin_bindings="Trainer.checkpoint_to_restore='${EXPROOT}/checkpoints/${EXPNAME}/model_${BESTNUM}.ckpt'" \
        --gin_bindings="Trainer.perform_filtration=${perform_filtration_ds}" \
        --gin_bindings="Learner.embedding_fn = @${_backbone}" \
        --gin_bindings="DataConfig.image_height=${image_height}" \
        --gin_bindings="Trainer.num_eval_episodes=$eval_episodes" \
        --gin_bindings="Trainer.test_entire_test_set_using_single_episode=True" \
        --gin_bindings="benchmark.eval_datasets='$DATASET'"      
    fi
  done
done