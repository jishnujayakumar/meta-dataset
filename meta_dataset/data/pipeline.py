# coding=utf-8
# Copyright 2021 The Meta-Dataset Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python2, python3
"""This module assembles full input data pipelines.

The whole pipeline incorporate (potentially) multiple Readers, the logic to
select between them, and the common logic to extract support / query sets if
needed, decode the example strings, and resize the images.
"""
# TODO(lamblinp): Organize the make_*_pipeline functions into classes, and
# make them output Batch or Episode objects directly.
# TODO(lamblinp): Update variable names to be more consistent
# - target, class_idx, label

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from email.mime import image

import functools

from absl import logging
import gin.tf
from meta_dataset import data
from meta_dataset.data import decoder
from meta_dataset.data import providers
from meta_dataset.data import learning_spec
from meta_dataset.data import reader
from meta_dataset.data import sampling
from simclr import data_util
from six.moves import zip
import tensorflow.compat.v1 as tf
import numpy as np

tf.flags.DEFINE_float('color_jitter_strength', 1.0,
                      'The strength of color jittering for SimCLR episodes.')


def filter_placeholders(example_strings, class_ids):
  """Returns tensors with only actual examples, filtering out the placeholders.

  Actual examples are the first ones in the tensors, and followed by placeholder
  ones, indicated by negative class IDs.

  Args:
    example_strings: 1-D Tensor of dtype str, Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset, except for negative ones, that indicate placeholder examples).
  """
  num_actual = tf.reduce_sum(tf.cast(class_ids >= 0, tf.int32))
  actual_example_strings = example_strings[:num_actual]
  actual_class_ids = class_ids[:num_actual]

  return (actual_example_strings, actual_class_ids)


def log_data_augmentation(data_augmentation, name):
  """Logs the given data augmentation parameters for diagnostic purposes."""
  if not data_augmentation:
    logging.info('No data augmentation provided for %s', name)
  else:
    logging.info('%s augmentations:', name)
    logging.info('enable_jitter: %s', data_augmentation.enable_jitter)
    logging.info('jitter_amount: %d', data_augmentation.jitter_amount)
    logging.info('enable_gaussian_noise: %s',
                 data_augmentation.enable_gaussian_noise)
    logging.info('gaussian_noise_std: %s', data_augmentation.gaussian_noise_std)


def flush_and_chunk_episode(example_strings, class_ids, chunk_sizes):
  """Removes flushed examples from an episode and chunks it.

  This function:

  1) splits the batch of examples into a "flush" chunk and some number of
     additional chunks (as determined by `chunk_sizes`),
  2) throws away the "flush" chunk, and
  3) removes the padded placeholder examples from the additional chunks.

  For example, in the context of few-shot learning, where episodes are composed
  of a support set and a query set, `chunk_size = (150, 100, 50)` would be
  interpreted as describing a "flush" chunk of size 150, a "support" chunk of
  size 100, and a "query" chunk of size 50.

  Args:
    example_strings: 1-D Tensor of dtype str, tf.train.Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset).
    chunk_sizes: tuple of ints representing the sizes of the flush and
      additional chunks.

  Returns:
    A tuple of episode chunks of the form `((chunk_0_example_strings,
    chunk_0_class_ids), (chunk_1_example_strings, chunk_1_class_ids), ...)`.
  """

  example_strings_chunks = tf.split(
      example_strings, num_or_size_splits=chunk_sizes)[1:]
  class_ids_chunks = tf.split(class_ids, num_or_size_splits=chunk_sizes)[1:]
  
  return tuple(
      filter_placeholders(strings, ids)
      for strings, ids in zip(example_strings_chunks, class_ids_chunks))


@gin.configurable(allowlist=['support_decoder', 'query_decoder'])
def process_dumped_episode(support_strings, query_strings, image_size,
                           support_decoder, query_decoder):
  """Processes a dumped episode.

  This function is almost like `process_episode()` function, except:
  - It doesn't need to call flush_and_chunk_episode().
  - And the labels are read from the tf.Example directly. We assume that
    labels are already mapped in to [0, n_ways - 1].

  Args:
    support_strings: 1-D Tensor of dtype str, Example protocol buffers of
      support set.
    query_strings: 1-D Tensor of dtype str, Example protocol buffers of query
      set.
    image_size: int, desired image size used during decoding.
    support_decoder: If ImageDecoder, used to decode support set images. If
      None, no decoding of support images is performed.
    query_decoder: ImageDecoder, used to decode query set images. If
      None, no decoding of query images is performed.

  Returns:
    support_images, support_labels, support_labels, query_images,
      query_labels, query_labels: Tensors, batches of images, labels, and
      labels, for the support and query sets (respectively). We return labels
      twice since dumped datasets doesn't have (absolute) class IDs anymore.
      Example proto buffers in place of images, and None in place of labels are
      returned if the corresponding decoder is None.


  """
  if isinstance(support_decoder, decoder.ImageDecoder):
    log_data_augmentation(support_decoder.data_augmentation, 'support')
    support_decoder.image_size = image_size

  if isinstance(query_decoder, decoder.ImageDecoder):
    log_data_augmentation(query_decoder.data_augmentation, 'query')
    query_decoder.image_size = image_size

  support_images = support_strings
  query_images = query_strings
  support_labels = None
  query_labels = None

  if support_decoder:
    support_images, support_labels = tf.map_fn(
        support_decoder.decode_with_label,
        support_strings,
        dtype=(support_decoder.out_type, tf.int32),
        back_prop=False)

  if query_decoder:
    query_images, query_labels = tf.map_fn(
        query_decoder.decode_with_label,
        query_strings,
        dtype=(query_decoder.out_type, tf.int32),
        back_prop=False)

  return (support_images, support_labels, support_labels, query_images,
          query_labels, query_labels)


def add_simclr_episodes(simclr_episode_fraction, *episode):
  """Convert simclr_episode_fraction of episodes into SimCLR Episodes."""

  def convert_to_simclr_episode(support_images=None,
                                support_labels=None,
                                support_class_ids=None,
                                query_images=None,
                                query_labels=None,
                                query_class_ids=None):
    """Convert a single episode into a SimCLR Episode."""

    # If there were k query examples of class c, keep the first k support
    # examples of class c as 'simclr' queries.  We do this by assigning an
    # id for each image in the query set, implemented as label*1e5+x+1, where
    # x is the number of images of the same label with a lower index within
    # the query set.  We do the same for the support set, which gives us a
    # mapping between query and support images which is injective (as long
    # as there's enough support-set images of each class).
    #
    # note: assumes max support label is 10000 - max_images_per_class
    query_idx_within_class = tf.cast(
        tf.equal(query_labels[tf.newaxis, :], query_labels[:, tf.newaxis]),
        tf.int32)
    query_idx_within_class = tf.linalg.diag_part(
        tf.cumsum(query_idx_within_class, axis=1))
    query_uid = query_labels * 10000 + query_idx_within_class
    support_idx_within_class = tf.cast(
        tf.equal(support_labels[tf.newaxis, :], support_labels[:, tf.newaxis]),
        tf.int32)
    support_idx_within_class = tf.linalg.diag_part(
        tf.cumsum(support_idx_within_class, axis=1))
    support_uid = support_labels * 10000 + support_idx_within_class

    # compute which support-set images have matches in the query set, and
    # discard the rest to produce the new query set.
    support_keep = tf.reduce_any(
        tf.equal(support_uid[:, tf.newaxis], query_uid[tf.newaxis, :]), axis=1)
    query_images = tf.boolean_mask(support_images, support_keep)

    support_labels = tf.range(
        tf.shape(support_labels)[0], dtype=support_labels.dtype)
    query_labels = tf.boolean_mask(support_labels, support_keep)
    query_class_ids = tf.boolean_mask(support_class_ids, support_keep)

    # Finally, apply SimCLR augmentation to all images.
    # Note simclr only blurs one image.
    query_images = simclr_augment(query_images, blur=True)
    support_images = simclr_augment(support_images)

    return (support_images, support_labels, support_class_ids, query_images,
            query_labels, query_class_ids)

  return tf.cond(
      tf.random_uniform([], minval=0, maxval=1,
                        dtype=tf.float32) > simclr_episode_fraction,
      lambda: episode, lambda: convert_to_simclr_episode(*episode))


def simclr_augment(image_batch, blur=False):
  """Apply simclr-style augmentations to a single set of images."""
  (h, w) = image_batch.shape.as_list()[1:3]
  image_batch = (image_batch + 1.0) / 2.0
  image_batch = tf.map_fn(
      lambda x: data_util.preprocess_for_train(x, h, w, impl='simclrv1'),
      image_batch)
  if blur:
    image_batch = tf.map_fn(lambda x: data_util.random_blur(x, h, w),
                            image_batch)
  image_batch = image_batch * 2.0 - 1.0
  return image_batch


@gin.configurable(allowlist=['support_decoder', 'query_decoder'])
def process_episode(example_strings, class_ids, dataset_name, data_split, 
                    perform_filtration, chunk_sizes, image_size, 
                    support_decoder, query_decoder, simclr_episode_fraction):
  """Processes an episode.

  This function:

  1) splits the batch of examples into "flush", "support", and "query" chunks,
  2) throws away the "flush" chunk,
  3) removes the padded placeholder examples from the "support" and "query"
     chunks,
  4) extracts and processes images out of the example strings, and
  5) builds support and query targets (numbers from 0 to K-1 where K is the
     number of classes in the episode) from the class IDs.

  Args:
    example_strings: 1-D Tensor of dtype str, tf.train.Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset).
    dataset_name: Name of the dataset, e.g. 'tesla'
    chunk_sizes: Tuple of ints representing the sizes the flush and additional
      chunks.
    image_size: int, desired image size used during decoding.
    support_decoder: Decoder, used to decode support set images. If
      None, no decoding of support images is performed.
    query_decoder: Decoder, used to decode query set images. If
      None, no decoding of query images is performed.
    simclr_episode_fraction: Fraction of episodes to convert to SimCLR episodes.

  Returns:
    support_images, support_labels, support_class_ids, query_images,
      query_labels, query_class_ids: Tensors, batches of images, labels, and
      (absolute) class IDs, for the support and query sets (respectively).
      Example proto buffers are returned in place of images if the corresponding
      decoder is None.

  """
  # TODO(goroshin): Replace with `support_decoder.log_summary(name='support')`.
  # TODO(goroshin): Eventually remove setting the image size here and pass it
  # to the ImageDecoder constructor instead.
  if isinstance(support_decoder, decoder.ImageDecoder):
    log_data_augmentation(support_decoder.data_augmentation, 'support')
    support_decoder.image_size = image_size
  if isinstance(query_decoder, decoder.ImageDecoder):
    log_data_augmentation(query_decoder.data_augmentation, 'query')
    query_decoder.image_size = image_size

  (support_strings, support_class_ids), (query_strings, query_class_ids) = \
    flush_and_chunk_episode(example_strings, class_ids, chunk_sizes)

  support_images = support_strings
  query_images = query_strings
  
  
  # UPDATE: Decode raw information of support example strings
  if support_decoder:
    support_images, support_labels, support_images_set = tf.map_fn(
        support_decoder.decode_with_label_and_set,
        support_strings,
        dtype=(support_decoder.out_type, tf.int32, tf.string),
        back_prop=False)

  # UDPATE: Decode raw information of query example strings
  if query_decoder:
    query_images, query_labels, query_images_set = tf.map_fn(
        query_decoder.decode_with_label_and_set,
        query_strings,
        dtype=(query_decoder.out_type, tf.int32, tf.string),
        back_prop=False)
  
  # UPDATE STARTS
  '''
  If dataset is 'tesla' and perform_fitration is True in gin file
  then do necessary filtration as follows:
    - support = pure-support + support_complement (query samples)
    - query = pure-query + query_complement (support samples)
    - swap support_complement with query complement by
       - keeping the cardinality constraints in mind
  '''

  perform_filtration = perform_filtration and \
                      support_decoder and query_decoder and dataset_name == 'tesla'
  
  if perform_filtration:
    # filter support artifacts (pure-support)
    support_keep = get_keep_boolean_mask(support_images_set, "support")
    support_keep_images = tf.boolean_mask(support_images, support_keep)
    support_keep_class_ids = tf.boolean_mask(support_class_ids, support_keep)
    
    # filter non support artifacts (support_complement)
    support_discard = tf.map_fn(tf.math.logical_not, support_keep, back_prop=False)
    support_discard_size = tf.reduce_sum(tf.cast(support_discard, tf.int32))

    support_discard_images = tf.boolean_mask(support_images, support_discard)
    support_discard_class_ids = tf.boolean_mask(support_class_ids, support_discard)

    # query artifact filter-1 based on image_set_info
    query_keep = get_keep_boolean_mask(query_images_set, "query")
    query_keep_images = tf.boolean_mask(query_images, query_keep)
    query_keep_class_ids = tf.boolean_mask(query_class_ids, query_keep)
    
    # filter query complement artifacts
    query_discard = tf.map_fn(tf.math.logical_not, query_keep, back_prop=False)
    query_discard_size = tf.reduce_sum(tf.cast(query_discard, tf.int32))
    query_discard_images = tf.boolean_mask(query_images, query_discard)
    query_discard_class_ids = tf.boolean_mask(query_class_ids, query_discard)

    # Minimum of support_discard_size and query_discard_size will be 
    # the number of samples which can be swaped/transfer from
    # support_complement to query_complement and vice-versa
    transfer_size = tf.math.minimum(support_discard_size, query_discard_size)
    indices = tf.transpose([tf.range(transfer_size)])

    is_support_discard_bigger = True if tf.math.greater(support_discard_size, transfer_size) else False
    is_query_discard_bigger = not is_support_discard_bigger

    # Get query_complement samples w.r.t. transfer_size
    if is_query_discard_bigger: #pick transfer_size elements
      query_discard_images = tf.gather_nd(query_discard_images,indices=indices)
      query_discard_class_ids = tf.gather_nd(query_discard_class_ids,indices=indices)
    
    # append query_complement to support artifacts
    support_keep_images = tf.concat([support_keep_images, query_discard_images], 0)
    support_keep_class_ids = tf.concat([support_keep_class_ids, query_discard_class_ids], 0)

    # get support_complement samples w.r.t. transfer_size
    if is_support_discard_bigger: #pick transfer_size elements
      support_discard_images = tf.gather_nd(support_discard_images,indices=indices)
      support_discard_class_ids = tf.gather_nd(support_discard_class_ids,indices=indices)
    
    # append support_complement to query artifacts
    query_keep_images = tf.concat([query_keep_images, support_discard_images], 0)
    query_keep_class_ids = tf.concat([query_keep_class_ids, support_discard_class_ids], 0)

    # get unique class ids from support class_ids after filtration
    unique_support_class_ids, _ = tf.unique(support_keep_class_ids)
    unique_query_class_ids, _ = tf.unique(query_keep_class_ids)
    
    if tf.math.not_equal(tf.size(unique_support_class_ids), tf.size(unique_query_class_ids)):
      is_present_map_fn = functools.partial(is_present, tensor=unique_support_class_ids)

      # get query_class_ids_present_in_support_class_ids after filtration
      query_class_ids_present_in_support_class_ids = tf.map_fn(
        is_present_map_fn,
        query_keep_class_ids,
        dtype=tf.bool,
        back_prop=False)

      # get the final boolean mask for query artifacts
      query_keep_images = tf.boolean_mask(query_keep_images, query_class_ids_present_in_support_class_ids)
      query_keep_class_ids = tf.boolean_mask(query_keep_class_ids, query_class_ids_present_in_support_class_ids) 
    
    # sync support and query class ids
    support_images, support_class_ids = get_sorted(support_keep_images, support_keep_class_ids)
    query_images, query_class_ids = get_sorted(query_keep_images, query_keep_class_ids)
    # UPDATE ENDS


  # Convert class IDs into labels in [0, num_ways).
  _, support_labels = tf.unique(support_class_ids)
  _, query_labels = tf.unique(query_class_ids)

  episode = (support_images, support_labels, support_class_ids, query_images,
             query_labels, query_class_ids)

  if simclr_episode_fraction > 0.0:
    episode = add_simclr_episodes(simclr_episode_fraction, *episode)

  return episode


# UPDATE
def get_sorted(images, class_ids):
  ''' 
  NOTE: this is required only when dealing with 'tesla' dataset
  Args:
    images: tensor containing images
    class_ids: tensor containing class_ids
  Returns: 
    Returns sorted images and class_ids 
  '''
  sorted_class_id_indices = tf.argsort(class_ids)
  sorted_images = tf.gather(images, indices=sorted_class_id_indices)
  sorted_class_ids = tf.gather(class_ids, indices=sorted_class_id_indices)
  return sorted_images, sorted_class_ids

# UPDATE
def get_keep_boolean_mask(image_set_info, class_set):
  '''
  Returns boolean mask of image_set_info tensor 
  using class_set: "support/query" info
  NOTE: this is required only when dealing with 'tesla' dataset
  Args:
    image_set_info: tensor containing corresdponding example string's set info
    class_set: string "support" or "query"
  Returns: 
    True/False depending on whether class_set matches image_set_info
  '''
  keep = tf.math.logical_or(
      tf.equal(image_set_info, tf.constant(class_set)),
      tf.equal(image_set_info, tf.constant(""))
  )
  return keep


# UPDATE
def is_present(x, tensor):
  '''
  Checks if x is present in a tensor
  Args:
    x: value to checked
    tensor: array in which x would be checked
  Returns: 
    True/False depending on whether x is present in tensor
  '''
  return tf.math.reduce_any(tf.equal(tensor, x))


@gin.configurable(allowlist=['batch_decoder'])
def process_batch(example_strings, class_ids, image_size, batch_decoder):
  """Processes a batch.

  This function:

  1) extracts and processes images out of the example strings.
  2) builds targets from the class ID and offset.

  Args:
    example_strings: 1-D Tensor of dtype str, Example protocol buffers.
    class_ids: 1-D Tensor of dtype int, class IDs (absolute wrt the original
      dataset).
    image_size: int, desired image size used during decoding.
    batch_decoder: Decoder class instance for the batch. If
      None, no decoding of the batch is performed.

  Returns:
    images, labels: Tensors, a batch of image and labels. Example proto buffers
    are returned in place of images if the batch decoder is None.
  """
  # TODO(goroshin): Replace with `batch_decoder.log_summary(name='support')`.
  if isinstance(batch_decoder, decoder.ImageDecoder):
    log_data_augmentation(batch_decoder.data_augmentation, 'batch')
    batch_decoder.image_size = image_size

  images = example_strings
  labels = class_ids
  
  if batch_decoder:
    images = tf.map_fn(
        batch_decoder,
        example_strings,
        dtype=batch_decoder.out_type,
        back_prop=False)

  return (images, labels)


# UPDATE: For test_entire_test_set_using_single_episode setup and 
# perform_filtration feature compatibility to the existing codebase. 
# NOTE: make_one_source_episode_pipeline() Tested. 
# This works without errors. For using only tesla, the following
# function is enough
def make_one_source_episode_pipeline(dataset_spec,
                                     use_dag_ontology,
                                     use_bilevel_ontology,
                                     split,
                                     episode_descr_config,
                                     test_entire_test_set_using_single_episode,
                                     perform_filtration=False,
                                     pool=None,
                                     shuffle_buffer_size=None,
                                     read_buffer_size_bytes=None,
                                     num_prefetch=0,
                                     image_size=None,
                                     num_to_take=None,
                                     ignore_hierarchy_probability=0.0,
                                     simclr_episode_fraction=0.0):
  """Returns a pipeline emitting data from one single source as Episodes.

  Args:
    dataset_spec: A DatasetSpecification object defining what to read from.
    use_dag_ontology: Whether to use source's ontology in the form of a DAG to
      sample episodes classes.
    use_bilevel_ontology: Whether to use source's bilevel ontology (consisting
      of superclasses and subclasses) to sample episode classes.
    split: A learning_spec.Split object identifying the source (meta-)split.
    episode_descr_config: An instance of EpisodeDescriptionConfig containing
      parameters relating to sampling shots and ways for episodes.
    test_entire_test_set_using_single_episode: A boolean flag indicating whether 
      the test has to be done using a single episode containing all support and 
      query images of the test set.
    perform_filtration: A boolean flag indicating whether filtration needs 
      to be performed for tesla dataset.
    pool: String (optional), for example-split datasets, which example split to
      use ('train', 'valid', or 'test'), used at meta-test time only.
    shuffle_buffer_size: int or None, shuffle buffer size for each Dataset.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    num_prefetch: int, the number of examples to prefetch for each class of each
      dataset. Prefetching occurs just after the class-specific Dataset object
      is constructed. If < 1, no prefetching occurs.
    image_size: int, desired image size used during decoding.
    num_to_take: Optional, an int specifying a number of elements to pick from
      each class' tfrecord. If specified, the available images of each class
      will be restricted to that int. By default no restriction is applied and
      all data is used.
    ignore_hierarchy_probability: Float, if using a hierarchy, this flag makes
      the sampler ignore the hierarchy for this proportion of episodes and
      instead sample categories uniformly.
    simclr_episode_fraction: Float, fraction of episodes that will be converted
      to SimCLR Episodes as described in the CrossTransformers paper.


  Returns:
    A Dataset instance that outputs tuples of fully-assembled and decoded
      episodes zipped with the ID of their data source of origin.
  """
  use_all_classes = test_entire_test_set_using_single_episode
  if pool is not None:
    if not data.POOL_SUPPORTED:
      raise NotImplementedError('Example-level splits or pools not supported.')
  if num_to_take is None:
    num_to_take = -1

  num_unique_descriptions = episode_descr_config.num_unique_descriptions

  episode_reader = reader.EpisodeReader(dataset_spec, split,
                                        shuffle_buffer_size,
                                        read_buffer_size_bytes, num_prefetch,
                                        num_to_take, num_unique_descriptions,
                                        test_entire_test_set_using_single_episode)
  sampler = sampling.EpisodeDescriptionSampler(
      episode_reader.dataset_spec,
      split,
      episode_descr_config,
      pool=pool,
      use_dag_hierarchy=use_dag_ontology,
      use_bilevel_hierarchy=use_bilevel_ontology,
      use_all_classes=use_all_classes,
      ignore_hierarchy_probability=ignore_hierarchy_probability,
      test_entire_test_set_using_single_episode=test_entire_test_set_using_single_episode)

  dataset = episode_reader.create_dataset_input_pipeline(sampler, pool=pool)

  # Episodes coming out of `dataset` contain flushed examples and are internally
  # padded with placeholder examples. `process_episode` discards flushed
  # examples, splits the episode into support and query sets, removes the
  # placeholder examples and decodes the example strings.
  chunk_sizes = sampler.compute_chunk_sizes()
  map_fn = functools.partial(
      process_episode,
      dataset_name=episode_reader.dataset_spec.name, #UPDATE,
      data_split=split,
      perform_filtration=perform_filtration,
      chunk_sizes=chunk_sizes,
      image_size=image_size,
      simclr_episode_fraction=simclr_episode_fraction)
  dataset = dataset.map(map_fn)
  # There is only one data source, so we know that all episodes belong to it,
  # but for interface consistency, zip with a dataset identifying the source.
  source_id_dataset = tf.data.Dataset.from_tensors(0).repeat()
  dataset = tf.data.Dataset.zip((dataset, source_id_dataset))

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset


# UPDATE: For test_entire_test_set_using_single_episode setup and 
# perform_filtration feature compatibility to the existing codebase. 
# NOTE: Testing required
# TODO: Logically and syntactically correct. Test if this works correctly
# Didn't test as for conducting experiments with tesla, only 
# make_one_source_episode_pipeline() was required
def make_multisource_episode_pipeline(dataset_spec_list,
                                      use_dag_ontology_list,
                                      use_bilevel_ontology_list,
                                      split,
                                      episode_descr_config,
                                      test_entire_test_set_using_single_episode,
                                      perform_filtration,
                                      pool=None,
                                      shuffle_buffer_size=None,
                                      read_buffer_size_bytes=None,
                                      num_prefetch=0,
                                      image_size=None,
                                      num_to_take=None,
                                      source_sampling_seed=None,
                                      simclr_episode_fraction=0.0):
  """Returns a pipeline emitting data from multiple sources as Episodes.

  Each episode only contains data from one single source. For each episode, its
  source is sampled uniformly across all sources.

  Args:
    dataset_spec_list: A list of DatasetSpecification, one for each source.
    use_dag_ontology_list: A list of Booleans, one for each source: whether to
      use that source's DAG-structured ontology to sample episode classes.
    use_bilevel_ontology_list: A list of Booleans, one for each source: whether
      to use that source's bi-level ontology to sample episode classes.
    split: A learning_spec.Split object identifying the sources split. It is the
      same for all datasets.
    episode_descr_config: An instance of EpisodeDescriptionConfig containing
      parameters relating to sampling shots and ways for episodes.
    test_entire_test_set_using_single_episode: A boolean flag indicating whether 
      the test has to be done using a single episode containing all support and 
      query images of the test set.
    perform_filtration: A boolean flag indicating whether filtration needs 
      to be performed for tesla dataset.
    pool: String (optional), for example-split datasets, which example split to
      use ('train', 'valid', or 'test'), used at meta-test time only.
    shuffle_buffer_size: int or None, shuffle buffer size for each Dataset.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    num_prefetch: int, the number of examples to prefetch for each class of each
      dataset. Prefetching occurs just after the class-specific Dataset object
      is constructed. If < 1, no prefetching occurs.
    image_size: int, desired image size used during decoding.
    num_to_take: Optional, a list specifying for each dataset the number of
      examples per class to restrict to (for this given split). If provided, its
      length must be the same as len(dataset_spec). If None, no restrictions are
      applied to any dataset and all data per class is used.
    source_sampling_seed: random seed for source sampling.
    simclr_episode_fraction: Float, fraction of episodes that will be converted
      to SimCLR Episodes as described in the CrossTransformers paper.

  Returns:
    A Dataset instance that outputs tuples of fully-assembled and decoded
      episodes zipped with the ID of their data source of origin.
  """
  # UPDATE
  use_all_classes = test_entire_test_set_using_single_episode

  if pool is not None:
    if not data.POOL_SUPPORTED:
      raise NotImplementedError('Example-level splits or pools not supported.')
  if num_to_take is not None and len(num_to_take) != len(dataset_spec_list):
    raise ValueError('num_to_take does not have the same length as '
                     'dataset_spec_list.')
  if num_to_take is None:
    num_to_take = [-1] * len(dataset_spec_list)
  num_unique_descriptions = episode_descr_config.num_unique_descriptions
  sources = []
  for source_id, (dataset_spec, use_dag_ontology, use_bilevel_ontology,
                  num_to_take_for_dataset) in enumerate(
                      zip(dataset_spec_list, use_dag_ontology_list,
                          use_bilevel_ontology_list, num_to_take)):
    episode_reader = reader.EpisodeReader(dataset_spec, split,
                                          shuffle_buffer_size,
                                          read_buffer_size_bytes, num_prefetch,
                                          num_to_take_for_dataset,
                                          num_unique_descriptions,
                                          test_entire_test_set_using_single_episode)
    sampler = sampling.EpisodeDescriptionSampler(
        episode_reader.dataset_spec,
        split,
        episode_descr_config,
        pool=pool,
        use_all_classes=use_all_classes,
        use_dag_hierarchy=use_dag_ontology,
        use_bilevel_hierarchy=use_bilevel_ontology,
        test_entire_test_set_using_single_episode=test_entire_test_set_using_single_episode)
    dataset = episode_reader.create_dataset_input_pipeline(sampler, pool=pool)
    # Create a dataset to zip with the above for identifying the source.
    source_id_dataset = tf.data.Dataset.from_tensors(source_id).repeat()
    sources.append(tf.data.Dataset.zip((dataset, source_id_dataset)))

  # Sample uniformly among sources.
  dataset = tf.data.experimental.sample_from_datasets(
      sources, seed=source_sampling_seed)

  # Episodes coming out of `dataset` contain flushed examples and are internally
  # padded with placeholder examples. `process_episode` discards
  # flushed examples, splits the episode into support and query sets, removes
  # the placeholder examples and decodes the example strings.
  chunk_sizes = sampler.compute_chunk_sizes()
  def map_fn(episode, source_id):
    return process_episode(
        *episode,
        dataset_name=episode_reader.dataset_spec.name, #UPDATE,
        data_split=split,
        perform_filtration=perform_filtration,
        chunk_sizes=chunk_sizes,
        image_size=image_size,
        simclr_episode_fraction=simclr_episode_fraction), source_id

  dataset = dataset.map(map_fn)

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset


def make_one_source_batch_pipeline(dataset_spec,
                                   split,
                                   batch_size,
                                   pool=None,
                                   shuffle_buffer_size=None,
                                   read_buffer_size_bytes=None,
                                   num_prefetch=0,
                                   image_size=None,
                                   num_to_take=None):
  """Returns a pipeline emitting data from one single source as Batches.

  Args:
    dataset_spec: A DatasetSpecification object defining what to read from.
    split: A learning_spec.Split object identifying the source split.
    batch_size: An int representing the max number of examples in each batch.
    pool: String (optional), for example-split datasets, which example split to
      use ('valid', or 'test'), used at meta-test time only.
    shuffle_buffer_size: int or None, number of examples in the buffer used for
      shuffling the examples from different classes, while they are mixed
      together. There is only one shuffling operation, not one per class.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    num_prefetch: int, the number of examples to prefetch for each class of each
      dataset. Prefetching occurs just after the class-specific Dataset object
      is constructed. If < 1, no prefetching occurs.
    image_size: int, desired image size used during decoding.
    num_to_take: Optional, an int specifying a number of elements to pick from
      each class' tfrecord. If specified, the available images of each class
      will be restricted to that int. By default no restriction is applied and
      all data is used.

  Returns:
    A Dataset instance that outputs decoded batches from all classes in the
    split.
  """
  if num_to_take is None:
    num_to_take = -1
  batch_reader = reader.BatchReader(dataset_spec, split, shuffle_buffer_size,
                                    read_buffer_size_bytes, num_prefetch,
                                    num_to_take)
  dataset = batch_reader.create_dataset_input_pipeline(
      batch_size=batch_size, pool=pool)
  map_fn = functools.partial(process_batch, image_size=image_size)
  dataset = dataset.map(map_fn)

  # There is only one data source, so we know that all batches belong to it,
  # but for interface consistency, zip with a dataset identifying the source.
  source_id_dataset = tf.data.Dataset.from_tensors(0).repeat()
  dataset = tf.data.Dataset.zip((dataset, source_id_dataset))

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset


# TODO(lamblinp): Update this option's name
@gin.configurable('BatchSplitReaderGetReader', allowlist=['add_dataset_offset'])
def make_multisource_batch_pipeline(dataset_spec_list,
                                    split,
                                    batch_size,
                                    add_dataset_offset,
                                    pool=None,
                                    shuffle_buffer_size=None,
                                    read_buffer_size_bytes=None,
                                    num_prefetch=0,
                                    image_size=None,
                                    num_to_take=None):
  """Returns a pipeline emitting data from multiple source as Batches.

  Args:
    dataset_spec_list: A list of DatasetSpecification, one for each source.
    split: A learning_spec.Split object identifying the source split.
    batch_size: An int representing the max number of examples in each batch.
    add_dataset_offset: A Boolean, whether to add an offset to each dataset's
      targets, so that each target is unique across all datasets.
    pool: String (optional), for example-split datasets, which example split to
      use ('valid', or 'test'), used at meta-test time only.
    shuffle_buffer_size: int or None, number of examples in the buffer used for
      shuffling the examples from different classes, while they are mixed
      together. There is only one shuffling operation, not one per class.
    read_buffer_size_bytes: int or None, buffer size for each TFRecordDataset.
    num_prefetch: int, the number of examples to prefetch for each class of each
      dataset. Prefetching occurs just after the class-specific Dataset object
      is constructed. If < 1, no prefetching occurs.
    image_size: int, desired image size used during decoding.
    num_to_take: Optional, a list specifying for each dataset the number of
      examples per class to restrict to (for this given split). If provided, its
      length must be the same as len(dataset_spec). If None, no restrictions are
      applied to any dataset and all data per class is used.

  Returns:
    A Dataset instance that outputs decoded batches from all classes in the
    split.
  """
  if num_to_take is not None and len(num_to_take) != len(dataset_spec_list):
    raise ValueError('num_to_take does not have the same length as '
                     'dataset_spec_list.')
  if num_to_take is None:
    num_to_take = [-1] * len(dataset_spec_list)
  sources = []
  offset = 0
  for source_id, (dataset_spec, num_to_take_for_dataset) in enumerate(
      zip(dataset_spec_list, num_to_take)):
    batch_reader = reader.BatchReader(dataset_spec, split, shuffle_buffer_size,
                                      read_buffer_size_bytes, num_prefetch,
                                      num_to_take_for_dataset)
    dataset = batch_reader.create_dataset_input_pipeline(
        batch_size=batch_size, pool=pool, offset=offset)
    # Create a dataset to zip with the above for identifying the source.
    source_id_dataset = tf.data.Dataset.from_tensors(source_id).repeat()
    sources.append(tf.data.Dataset.zip((dataset, source_id_dataset)))
    if add_dataset_offset:
      offset += len(dataset_spec.get_classes(split))

  # Sample uniformly among sources
  dataset = tf.data.experimental.sample_from_datasets(sources)

  def map_fn(batch, source_id):
    return process_batch(*batch, image_size=image_size), source_id

  dataset = dataset.map(map_fn)

  # Overlap episode processing and training.
  dataset = dataset.prefetch(1)
  return dataset
