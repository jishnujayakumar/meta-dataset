#!/bin/bash
source __select_best_model.sh $1 $2 $3 $4 $5 $7 $8

# link dataset variant of choice, useful for tesla
export TESLA_DATASET_VARIANT=$6

eval_episodes=1 #600
backbone=$8
_backbone=$backbone
if test "$backbone" = "convnet"
then
    _backbone="four_layer_convnet"
fi

if test "$backbone" = "resnet34_ctx"
then
    _backbone="resnet34"
fi

cd $RECORDS; rm tesla; ln -s $TESLA_DATASET_VARIANT tesla; cd $ROOT_DIR;

ls -l $RECORDS # useful to check if sym links are correct

for MODEL in $models
do
  export EXP_GIN=${MODEL}_${SOURCE}
  # export EXPNAME=${EXP_GIN}${nve_suffix}
  # for DATASET in omniglot 
  for DATASET in tesla 
  do
    echo "MODEL-FILTER: $perform_filtration_model"
    echo "DATASET-FILTER: $perform_filtration_ds"
    if test "$backbone" = "" # default backbone
    then
      # set BESTNUM to the "best_update_num" field in the corresponding best_....txt
      export BESTNUM=$9
      python -m meta_dataset.train \
        --is_training=False \
        --records_root_dir=$RECORDS \
        --summary_dir=${EXPROOT}/summaries/${EXPNAME} \
        --gin_config=meta_dataset/learn/gin/best/${EXP_GIN}.gin \
        --gin_bindings="Trainer.experiment_name='${EXPNAME}'" \
        --gin_bindings="Trainer.checkpoint_to_restore='${EXPROOT}/checkpoints/${EXPNAME}/model_${BESTNUM}.ckpt'" \
        --gin_bindings="Trainer.perform_filtration=${perform_filtration_ds}" \
        --gin_bindings="DataConfig.image_height=126" \
        --gin_bindings="EpisodeDescriptionConfig.num_support=9" \
        --gin_bindings="EpisodeDescriptionConfig.num_ways=5" \
        --gin_bindings="EpisodeDescriptionConfig.num_query=5" \
        --gin_bindings="Trainer.num_eval_episodes=$eval_episodes" \
        --gin_bindings="Trainer.test_entire_test_set_using_single_episode=False" \
        --gin_bindings="benchmark.eval_datasets='$DATASET'"
    else
      export BESTNUM=$9
      python -m meta_dataset.train \
        --is_training=False \
        --records_root_dir=$RECORDS \
        --summary_dir=${EXPROOT}/summaries/${EXPNAME} \
        --gin_config=meta_dataset/learn/gin/best/${EXP_GIN}.gin \
        --gin_bindings="Trainer.experiment_name=''" \
        --gin_bindings="Trainer.checkpoint_to_restore='${EXPROOT}/checkpoints/${EXPNAME}/model_${BESTNUM}.ckpt'" \
        --gin_bindings="Trainer.perform_filtration=${perform_filtration_ds}" \
        --gin_bindings="Learner.embedding_fn = @${_backbone}" \
        --gin_bindings="DataConfig.image_height=126" \
        --gin_bindings="EpisodeDescriptionConfig.num_support=9" \
        --gin_bindings="EpisodeDescriptionConfig.num_ways=5" \
        --gin_bindings="EpisodeDescriptionConfig.num_query=5" \
        --gin_bindings="Trainer.num_eval_episodes=$eval_episodes" \
        --gin_bindings="Trainer.test_entire_test_set_using_single_episode=False" \
        --gin_bindings="benchmark.eval_datasets='$DATASET'"      
    fi
  done
done
