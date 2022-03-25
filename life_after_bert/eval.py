import logging
import os
import sys

import torch
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import life_after_bert as LaB


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(os.path.basename(__file__))


def evaluate_encoder(model, tokenizer, eval_dataset, device="cpu", batch_size=16, output_predictions=True, progress_bar=True):
    """
    Evaluates any HuggingFace Encoder model.

    A model's prediction is determined by the probabilities assigned to the mask token.
    The answer choice with the highest probability is selected chosen as the prediction.
    See Section 3.2 -- "MC-MLM vs. MC-QA" -- for more details.

    Args:
        model:
            Any pretrained transformers.AutoModelForMaskedLM()
        tokenizer:
            Tokenizer for model
            As eval_dataset already has tokenized input ids, tokenizer is only used for determining the mask token.  # TODO: pass in mask_token_id instead of tokenizer?
        eval_dataset:
            life_after_bert.MCDataset()
        device:
            "cuda" or "cpu"  # TODO: cuda:0/cuda:1?
        batch_size:
            # TODO: distributed
        output_predictions:
            Whether or not to return predictions and labels
        progress_bar:
            Whether or not to use tqdm progress bar

    Returns: accuracy, (answers, preds)
        accuracy - model accuracy on task
        answers - tensor containing ground truths, only returned if output_predictions=True
        preds - tensor containing model predictions, only returned if output_predictions=True
    """

    eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size, collate_fn=LaB.collate_fn, shuffle=False)
    mask_id = tokenizer.mask_token_id
    all_answers, all_preds = [], []

    model.to(device)
    model.eval()
    if eval_dataset.task_type == "oLMpics MLM":
        for batch in tqdm(eval_dataloader, desc="Evaluating", disable=not progress_bar):
            all_answers.extend(batch["choice_ids"].gather(1, batch["answer_id"].unsqueeze(1)).squeeze(1).tolist())
            choice_ids = batch.pop("choice_ids")
            del batch["answer_id"]

            for key, value in batch.items():
                batch[key] = value.to(device)

            with torch.no_grad():
                outputs = model(**batch)

                logits = outputs.logits

                mask_indices = torch.tensor([input_ids.tolist().index(mask_id) for input_ids in batch["input_ids"]], device=device)
                mask_logits = logits.gather(1, mask_indices.view(-1, 1, 1).expand(-1, 1, logits.size()[-1])).squeeze(1)
                choice_logits = mask_logits.gather(1, choice_ids.to(device))
                max_inds = torch.argmax(choice_logits, dim=1).cpu()
                all_preds.extend(choice_ids.gather(1, max_inds.unsqueeze(1)).squeeze(1).tolist())

        all_answers = torch.tensor(all_answers)  # TODO: only track if output_predictions?
        all_preds = torch.tensor(all_preds)
        output = ((all_answers.numpy() == all_preds.numpy()).mean(),)
        if output_predictions:
            output += ((all_answers, all_preds),)

        return output
    else:
        raise NotImplementedError


def evaluate_decoder(model, tokenizer, eval_dataset, device="cpu", batch_size=16, output_predictions=True, progress_bar=True):
    """
    Evaluates any HuggingFace Decoder model.

    To allow for bidirectionality, the mask token is replaced with each answer choice
    And the sum of the log probabilities is calculated for each sentence.
    A model's prediction is the choice with the highest total probability.
    See Section 3.2 -- "Extending Beyond MLM" -- for more details.

    Args:
        model:
            Any pretrained transformers.AutoModelForCausalLM()
        tokenizer:
            Tokenizer for model
            As eval_dataset already has tokenized input ids, tokenizer is only used for determining the mask token
        eval_dataset:
            life_after_bert.MCDataset()
        device:
            "cuda" or "cpu"
        batch_size:

        output_predictions:
            Whether or not to return predictions and labels
        progress_bar:
            Whether or not to use tqdm progress bar

    Returns: accuracy, (answers, preds)
        accuracy - model accuracy on task
        answers - tensor containing ground truths, only returned if output_predictions=True
        preds - tensor containing model predictions, only returned if output_predictions=True
    """

    eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size, collate_fn=LaB.collate_fn, shuffle=False)
    all_answers, all_preds = [], []

    model.to(device)
    model.eval()
    if eval_dataset.task_type == "oLMpics MLM":
        for batch in tqdm(eval_dataloader, desc="Evaluating", disable=not progress_bar):
            mask_indices = [input_ids.tolist().index(tokenizer.mask_token_id) for input_ids in batch["input_ids"]]
            eos_indices = [input_ids.tolist().index(tokenizer.eos_token_id) for input_ids in batch["input_ids"]]

            all_answers.extend(batch["choice_ids"].gather(1, batch["answer_id"].unsqueeze(1)).squeeze(1).tolist())
            choice_ids = batch.pop("choice_ids")
            del batch["answer_id"]

            for key, value in batch.items():
                batch[key] = value.to(device)

            choice_probs = []
            for choice_index in range(eval_dataset.num_choices):
                for batch_index in range(len(mask_indices)):  # TODO: remove loop
                    batch["input_ids"][batch_index][mask_indices[batch_index]] = choice_ids[batch_index][choice_index]

                with torch.no_grad():
                    outputs = model(**batch)

                logits = outputs.logits
                sentence_probs = LaB.get_sentence_prob(batch["input_ids"], logits, eos_indices)
                choice_probs.append(sentence_probs)

            choice_probs = torch.stack(choice_probs, dim=1)
            max_inds = torch.argmax(choice_probs, dim=1).cpu()

            all_preds.extend(choice_ids.gather(1, max_inds.unsqueeze(1)).squeeze(1).tolist())

        all_answers = torch.tensor(all_answers)
        all_preds = torch.tensor(all_preds)

        output = ((all_answers.numpy() == all_preds.numpy()).mean(),)
        if output_predictions:
            output += ((all_answers, all_preds),)

        return output
    else:
        raise NotImplementedError


def evaluate_encoder_decoder(model, eval_dataset, static_decoder_input_ids, device="cpu", batch_size=16, output_predictions=True, progress_bar=True):
    """
    Evaluates any HuggingFace Encoder Decoder model.

    A model's prediction is determined by the probabilities assigned to the mask token.  # TODO: exact same as evaluate_encoder()
    The answer choice with the highest probability is selected chosen as the prediction.
    See Section 3.2 -- "MC-MLM vs. MC-QA" -- for more details.

    Args:
        model:
            Any pretrained transformers.T5ForConditionalGeneration()  # TODO: BART
        eval_dataset:
            life_after_bert.MCDataset()
        static_decoder_input_ids:
            torch.tensor() with the token ids of the decoder prompt
            E.g. "<pad> <extra_id_0" for T5, because T5 reuses the pad token for the decoder start generation token
        device:
            "cuda" or "cpu"  # TODO: cuda:0/cuda:1?
        batch_size:
            # TODO: distributed
        output_predictions:
            Whether or not to return predictions and labels
        progress_bar:
            Whether or not to use tqdm progress bar

    Returns: accuracy, (answers, preds)
        accuracy - model accuracy on task
        answers - tensor containing ground truths, only returned if output_predictions=True
        preds - tensor containing model predictions, only returned if output_predictions=True
    """

    eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size, collate_fn=LaB.collate_fn, shuffle=False)
    all_answers, all_preds = [], []

    model.to(device)
    model.eval()
    if eval_dataset.task_type == "oLMpics MLM":
        for batch in tqdm(eval_dataloader, desc="Evaluating", disable=not progress_bar):
            all_answers.extend(batch["choice_ids"].gather(1, batch["answer_id"].unsqueeze(1)).squeeze(1).tolist())
            choice_ids = batch.pop("choice_ids")
            del batch["answer_id"]

            for key, value in batch.items():
                batch[key] = value.to(device)

            with torch.no_grad():
                outputs = model(
                    **batch, decoder_input_ids=static_decoder_input_ids.expand(len(batch["input_ids"]), -1).to(device)
                )

                logits = outputs.logits
                choice_logits = logits[:, 1, :].gather(1, choice_ids.to(device))
                max_inds = torch.argmax(choice_logits, dim=1).cpu()
                all_preds.extend(choice_ids.gather(1, max_inds.unsqueeze(1)).squeeze(1).tolist())

        all_answers = torch.tensor(all_answers)
        all_preds = torch.tensor(all_preds)

        output = ((all_answers.numpy() == all_preds.numpy()).mean(),)
        if output_predictions:
            output += ((all_answers, all_preds),)

        return output
    else:
        raise NotImplementedError


class LaBEvaluator:
    """
    Evaluates model on all zero-shot oLMpics MLM tasks.  # TODO: more tasks
    Constructor takes in no arguments, `evaluator = LaBEvaluator()`
    """
    ARCH_TO_FUNCTION = {
        "encoder": evaluate_encoder,
        "decoder": evaluate_decoder,
        "encoder decoder": evaluate_encoder_decoder
    }

    def evaluate(self, model, tokenizer, task_infos, model_arch, device="cpu", batch_size=16, task_type="oLMpics MLM",
                 output_predictions=False, progress_bar=True):

        eval_fn = self.ARCH_TO_FUNCTION[model_arch.lower()]
        for i, (task_name, num_choices) in enumerate(task_infos):
            dataset = LaB.MCDataset.load_data(task_name, num_choices, task_type, tokenizer)
            if model_arch.lower() == "encoder decoder":
                if i == 0:
                    logger.warning("Assuming T5.")

                static_decoder_input_ids = tokenizer("<pad> <extra_id_0>", add_special_tokens=False,
                                                     return_tensors="pt").input_ids
                results = eval_fn(model, dataset, static_decoder_input_ids, device=device, batch_size=batch_size, output_predictions=output_predictions, progress_bar=progress_bar)
            else:
                results = eval_fn(model, tokenizer, dataset, device=device, batch_size=batch_size,
                                  output_predictions=output_predictions, progress_bar=progress_bar)

            logger.info(f"Accuracy on {task_name}: {results[0]}")  # TODO: return (all_answers, all_preds)
