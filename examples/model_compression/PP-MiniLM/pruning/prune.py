# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import argparse
import logging
import os
import sys
import random
import time
import math
from functools import partial

import numpy as np
import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from paddle.io import DataLoader

from paddlenlp.data import Stack, Tuple, Pad, Dict
from paddlenlp.datasets import load_dataset
from paddlenlp.transformers import LinearDecayWithWarmup
from paddlenlp.utils.log import logger
from paddlenlp.transformers import ErnieForSequenceClassification, ErnieTokenizer, ErnieModel

from paddleslim.nas.ofa import OFA, DistillConfig, utils
from paddleslim.nas.ofa.utils import nlp_utils
from paddleslim.nas.ofa.convert_super import Convert, supernet

sys.path.append("../")
from data import convert_example, METRIC_CLASSES, MODEL_CLASSES


def parse_args():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--task_name",
        default=None,
        type=str,
        required=True,
        help="The name of the task to train selected in the list: " +
        ", ".join(METRIC_CLASSES.keys()), )
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: " +
        ", ".join(MODEL_CLASSES.keys()), )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: "
        + ", ".join(
            sum([
                list(classes[-1].pretrained_init_configuration.keys())
                for classes in MODEL_CLASSES.values()
            ], [])), )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--glue_dir",
        default="/root/.paddlenlp/datasets/Clue/",
        type=str,
        required=False,
        help="The Glue directory.", )
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
        "than this will be truncated, sequences shorter will be padded.", )
    parser.add_argument(
        "--batch_size",
        default=8,
        type=int,
        help="Batch size per GPU/CPU for training.", )
    parser.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam.")
    parser.add_argument(
        "--weight_decay",
        default=0.0,
        type=float,
        help="Weight decay if we apply some.")
    parser.add_argument(
        "--adam_epsilon",
        default=1e-8,
        type=float,
        help="Epsilon for Adam optimizer.")
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument(
        "--lambda_logit",
        default=1.0,
        type=float,
        help="lambda for logit loss.")
    parser.add_argument(
        "--num_train_epochs",
        default=3,
        type=int,
        help="Total number of training epochs to perform.", )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--warmup_steps",
        default=0,
        type=int,
        help="Linear warmup over warmup_steps.")
    parser.add_argument(
        "--warmup_proportion",
        default=0.1,
        type=float,
        help="Linear warmup proportion over total steps.")
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=500,
        help="Log every X updates steps.")
    parser.add_argument(
        "--save_steps",
        type=int,
        default=500,
        help="Save checkpoint every X updates steps.")
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for initialization")
    parser.add_argument(
        "--device",
        default="gpu",
        type=str,
        choices=["gpu", "cpu", "xpu"],
        help="The device to select to train the model, is must be cpu/gpu/xpu.")
    parser.add_argument(
        '--width_mult_list',
        nargs='+',
        type=float,
        default=[1.0, 5 / 6, 2 / 3, 0.5],
        help="width mult in compress")
    args = parser.parse_args()
    return args


def set_seed(args):
    # Use the same data seed(for data shuffle) for all procs to guarantee data
    # consistency after sharding.
    random.seed(args.seed)
    np.random.seed(args.seed)
    # Maybe different op seeds(for dropout) for different procs is better. By:
    # `paddle.seed(args.seed + paddle.distributed.get_rank())`
    paddle.seed(args.seed)


@paddle.no_grad()
def evaluate(model, metric, data_loader, width_mult, student=False):
    model.eval()
    metric.reset()
    for i, batch in enumerate(data_loader):
        input_ids, segment_ids, labels = batch
        logits = model(input_ids, segment_ids, attention_mask=[None, None])
        if isinstance(logits, tuple):
            logits = logits[0]
        correct = metric.compute(logits, labels)
        metric.update(correct)

    res = metric.accumulate()
    print("width_mult: %s, acc: %s, " % (str(width_mult), res), end='')
    model.train()
    return res


### monkey patch for bert forward to accept [attention_mask, head_mask] as  attention_mask
def ernie_forward(self,
                  input_ids,
                  token_type_ids=None,
                  position_ids=None,
                  attention_mask=[None, None]):
    wtype = self.pooler.dense.fn.weight.dtype if hasattr(
        self.pooler.dense, 'fn') else self.pooler.dense.weight.dtype
    if attention_mask[0] is None:
        attention_mask[0] = paddle.unsqueeze(
            (input_ids == self.pad_token_id).astype(wtype) * -1e9, axis=[1, 2])
    embedding_output = self.embeddings(input_ids, token_type_ids, position_ids)
    encoded_layer = self.encoder(embedding_output, attention_mask)
    pooled_output = self.pooler(encoded_layer)

    return encoded_layer, pooled_output


ErnieModel.forward = ernie_forward


### reorder weights according head importance and neuron importance
def reorder_neuron_head(model, head_importance, neuron_importance):
    # reorder heads and ffn neurons
    for layer, current_importance in enumerate(neuron_importance):
        # reorder heads
        idx = paddle.argsort(head_importance[layer], descending=True)
        nlp_utils.reorder_head(model.ernie.encoder.layers[layer].self_attn, idx)
        # reorder neurons
        idx = paddle.argsort(
            paddle.to_tensor(current_importance), descending=True)
        nlp_utils.reorder_neuron(
            model.ernie.encoder.layers[layer].linear1.fn, idx, dim=1)
        nlp_utils.reorder_neuron(
            model.ernie.encoder.layers[layer].linear2.fn, idx, dim=0)


def soft_cross_entropy(inp, target):
    inp_likelihood = F.log_softmax(inp, axis=-1)
    target_prob = F.softmax(target, axis=-1)
    return -1. * paddle.mean(paddle.sum(inp_likelihood * target_prob, axis=-1))


def do_train(args):
    paddle.set_device(args.device)
    if paddle.distributed.get_world_size() > 1:
        paddle.distributed.init_parallel_env()

    set_seed(args)

    args.task_name = args.task_name.lower()
    metric_class = METRIC_CLASSES[args.task_name]
    args.model_type = args.model_type.lower()
    model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    train_ds = load_dataset('clue', args.task_name, splits='train')
    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path)

    trans_func = partial(
        convert_example,
        tokenizer=tokenizer,
        label_list=train_ds.label_list,
        max_seq_length=args.max_seq_length)
    train_ds = train_ds.map(trans_func, lazy=True)
    train_batch_sampler = paddle.io.DistributedBatchSampler(
        train_ds, batch_size=args.batch_size, shuffle=True)
    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # input
        Pad(axis=0, pad_val=tokenizer.pad_token_type_id),  # segment
        Stack(dtype="int64" if train_ds.label_list else "float32")  # label
    ): fn(samples)

    train_data_loader = DataLoader(
        dataset=train_ds,
        batch_sampler=train_batch_sampler,
        collate_fn=batchify_fn,
        num_workers=0,
        return_list=True)

    dev_ds = load_dataset('clue', args.task_name, splits='dev')
    dev_ds = dev_ds.map(trans_func, lazy=True)
    dev_batch_sampler = paddle.io.BatchSampler(
        dev_ds, batch_size=args.batch_size, shuffle=False)
    dev_data_loader = DataLoader(
        dataset=dev_ds,
        batch_sampler=dev_batch_sampler,
        collate_fn=batchify_fn,
        num_workers=0,
        return_list=True)
    num_labels = 1 if train_ds.label_list == None else len(train_ds.label_list)

    model = model_class.from_pretrained(
        args.model_name_or_path, num_classes=num_labels)

    # Step1: Initialize a dictionary to save the weights from the origin BERT model.
    origin_weights = model.state_dict()

    # Step2: Convert origin model to supernet.
    sp_config = supernet(expand_ratio=[1.0])
    model = Convert(sp_config).convert(model)
    # Use weights saved in the dictionary to initialize supernet.
    utils.set_state_dict(model, origin_weights)
    del origin_weights

    super_sd = paddle.load(
        os.path.join(args.model_name_or_path, 'model_state.pdparams'))
    model.set_state_dict(super_sd)

    # Step3: Define teacher model.
    teacher_model = model_class.from_pretrained(
        args.model_name_or_path, num_classes=num_labels)

    # Step4: Config about distillation.
    mapping_layers = ['ernie.embeddings']
    for idx in range(model.ernie.config['num_hidden_layers']):
        mapping_layers.append('ernie.encoder.layers.{}'.format(idx))

    default_distill_config = {
        'lambda_distill': 0.1,
        'teacher_model': teacher_model,
        'mapping_layers': mapping_layers,
    }
    distill_config = DistillConfig(**default_distill_config)

    # Step5: Config in supernet training.
    ofa_model = OFA(model,
                    distill_config=distill_config,
                    elastic_order=['width'])

    criterion = paddle.nn.loss.CrossEntropyLoss(
    ) if train_ds.label_list else paddle.nn.loss.MSELoss()

    metric = metric_class()

    #### Step6: Calculate the importance of neurons and head,
    #### and then reorder them according to the importance.
    head_importance, neuron_importance = nlp_utils.compute_neuron_head_importance(
        args.task_name,
        ofa_model.model,
        dev_data_loader,
        loss_fct=criterion,
        num_layers=model.ernie.config['num_hidden_layers'],
        num_heads=model.ernie.config['num_attention_heads'])
    reorder_neuron_head(ofa_model.model, head_importance, neuron_importance)

    if paddle.distributed.get_world_size() > 1:
        ofa_model.model = paddle.DataParallel(ofa_model.model)

    if args.max_steps > 0:
        num_training_steps = args.max_steps
        num_train_epochs = math.ceil(num_training_steps /
                                     len(train_data_loader))
    else:
        num_training_steps = len(train_data_loader) * args.num_train_epochs
        num_train_epochs = args.num_train_epochs

    warmup = args.warmup_steps if args.warmup_steps > 0 else args.warmup_proportion

    lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps,
                                         warmup)

    # Generate parameter names needed to perform weight decay.
    # All bias and LayerNorm parameters are excluded.
    decay_params = [
        p.name for n, p in model.named_parameters()
        if not any(nd in n for nd in ["bias", "norm"])
    ]

    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_scheduler,
        beta1=0.9,
        beta2=0.999,
        epsilon=args.adam_epsilon,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        apply_decay_param_fun=lambda x: x in decay_params,
        grad_clip=nn.ClipGradByGlobalNorm(args.max_grad_norm))

    global_step = 0
    tic_train = time.time()
    best_res = 0.0
    for epoch in range(num_train_epochs):
        # Step7: Set current epoch and task.
        ofa_model.set_epoch(epoch)
        ofa_model.set_task('width')

        for step, batch in enumerate(train_data_loader):
            global_step += 1
            input_ids, segment_ids, labels = batch

            for width_mult in args.width_mult_list:
                # Step8: Broadcast supernet config from width_mult,
                # and use this config in supernet training.
                net_config = utils.dynabert_config(ofa_model, width_mult)
                ofa_model.set_net_config(net_config)
                logits, teacher_logits = ofa_model(
                    input_ids, segment_ids, attention_mask=[None, None])
                rep_loss = ofa_model.calc_distill_loss()
                logit_loss = soft_cross_entropy(logits, teacher_logits.detach())
                loss = rep_loss + args.lambda_logit * logit_loss
                loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.clear_grad()

            if global_step % args.logging_steps == 0:
                logger.info(
                    "global step %d, epoch: %d, batch: %d, loss: %f, speed: %.2f step/s"
                    % (global_step, epoch, step, loss,
                       args.logging_steps / (time.time() - tic_train)))
                tic_train = time.time()

            if global_step % args.save_steps == 0 or global_step == num_training_steps:
                tic_eval = time.time()
                evaluate(teacher_model, metric, dev_data_loader, width_mult=100)
                print("eval done total : %s s" % (time.time() - tic_eval))
                for idx, width_mult in enumerate(args.width_mult_list):
                    net_config = utils.dynabert_config(ofa_model, width_mult)
                    ofa_model.set_net_config(net_config)
                    tic_eval = time.time()
                    res = evaluate(ofa_model, metric, dev_data_loader,
                                   width_mult)
                    print("eval done total : %s s" % (time.time() - tic_eval))

                    if best_res < res:
                        output_dir = args.output_dir
                        if not os.path.exists(output_dir):
                            os.makedirs(output_dir)
                        # need better way to get inner model of DataParallel
                        model_to_save = model._layers if isinstance(
                            model, paddle.DataParallel) else model
                        model_to_save.save_pretrained(output_dir)
                        tokenizer.save_pretrained(output_dir)
                        best_res = res
            if global_step >= num_training_steps:
                return
    print("best_res: ", best_res)


def print_arguments(args):
    """print arguments"""
    print('-----------  Configuration Arguments -----------')
    for arg, value in sorted(vars(args).items()):
        print('%s: %s' % (arg, value))
    print('------------------------------------------------')


if __name__ == "__main__":
    args = parse_args()
    print_arguments(args)
    do_train(args)
