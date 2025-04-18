import re
import tempfile

import click
import gradio as gr
import numpy as np
import soundfile as sf
import torchaudio
from cached_path import cached_path
from transformers import AutoModelForCausalLM, AutoTokenizer
from num2words import num2words

try:
    import spaces

    USING_SPACES = True
except ImportError:
    USING_SPACES = False


def gpu_decorator(func):
    if USING_SPACES:
        return spaces.GPU(func)
    else:
        return func


from f5_tts.model import DiT, UNetT
from f5_tts.infer.utils_infer import (
    load_vocoder,
    load_model,
    preprocess_ref_audio_text,
    infer_process,
    remove_silence_for_generated_wav,
    save_spectrogram,
)

vocoder = load_vocoder()


# load models
F5TTS_model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
F5TTS_ema_model = load_model(
    DiT, F5TTS_model_cfg, str(cached_path("hf://jpgallegoar/F5-Spanish/model_1200000.safetensors"))
)

chat_model_state = None
chat_tokenizer_state = None


@gpu_decorator
def generate_response(messages, model, tokenizer):
    """Generate response using Qwen"""
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )

    model_inputs = tokenizer([text], return_tensors="pt").to(model.device)
    generated_ids = model.generate(
        **model_inputs,
        max_new_tokens=512,
        temperature=0.7,
        top_p=0.95,
    )

    generated_ids = [
        output_ids[len(input_ids) :] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]
    return tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]

def traducir_numero_a_texto(texto):
    texto_separado = re.sub(r'([A-Za-z])(\d)', r'\1 \2', texto)
    texto_separado = re.sub(r'(\d)([A-Za-z])', r'\1 \2', texto_separado)
    
    def reemplazar_numero(match):
        numero = match.group()
        return num2words(int(numero), lang='es')

    texto_traducido = re.sub(r'\b\d+\b', reemplazar_numero, texto_separado)

    return texto_traducido

@gpu_decorator
def infer(
    ref_audio_orig, ref_text, gen_text, model, remove_silence, cross_fade_duration=0.15, speed=1, show_info=gr.Info
):
    ref_audio, ref_text = preprocess_ref_audio_text(ref_audio_orig, ref_text, show_info=show_info)

    ema_model = F5TTS_ema_model

    if not gen_text.startswith(" "):
        gen_text = " " + gen_text
    if not gen_text.endswith(". "):
        gen_text += ". "

    gen_text = gen_text.lower()
    gen_text = traducir_numero_a_texto(gen_text)

    final_wave, final_sample_rate, combined_spectrogram = infer_process(
        ref_audio,
        ref_text,
        gen_text,
        ema_model,
        vocoder,
        cross_fade_duration=cross_fade_duration,
        speed=speed,
        show_info=show_info,
        progress=gr.Progress(),
    )

    # Remove silence
    if remove_silence:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            sf.write(f.name, final_wave, final_sample_rate)
            remove_silence_for_generated_wav(f.name)
            final_wave, _ = torchaudio.load(f.name)
        final_wave = final_wave.squeeze().cpu().numpy()

    # Save the spectrogram
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_spectrogram:
        spectrogram_path = tmp_spectrogram.name
        save_spectrogram(combined_spectrogram, spectrogram_path)

    return (final_sample_rate, final_wave), spectrogram_path


def parse_speechtypes_text(gen_text):
    # Pattern to find {speechtype}
    pattern = r"\{(.*?)\}"

    # Split the text by the pattern
    tokens = re.split(pattern, gen_text)

    segments = []

    current_style = "Regular"

    for i in range(len(tokens)):
        if i % 2 == 0:
            # This is text
            text = tokens[i].strip()
            if text:
                segments.append({"style": current_style, "text": text})
        else:
            # This is style
            style = tokens[i].strip()
            current_style = style

    return segments


########## INICIA GRADIO PRINCIPAL ##########
with gr.Blocks() as app_chat:
    gr.Markdown(
        """
# Mnemosynth
1. Carga el modelo de chat.
2. Sube un clip de audio de referencia y opcionalmente su transcripción. Para los mejores resultados, intenta convertir tu audio de referencia a WAV o MP3, asegurarte de que duren entre 11 y 14 segundos, que comiencen y acaben con entre medio segundo y un segundo de silencio, y a ser posible que acabe con el final de la frase.
3. Graba tu mensaje a través de tu micrófono o escribe tu pregunta.
4. La IA responderá usando la voz de referencia.

NOTA: El modelo actualmente sólo soporta el castellano.
"""
    )

    if not USING_SPACES:
        load_chat_model_btn = gr.Button("Cargar Modelo de Chat", variant="primary")

        chat_interface_container = gr.Column(visible=False)

        @gpu_decorator
        def load_chat_model():
            global chat_model_state, chat_tokenizer_state
            if chat_model_state is None:
                show_info = gr.Info
                show_info("Cargando modelo de chat...")
                model_name = "Qwen/Qwen2.5-3B-Instruct"
                chat_model_state = AutoModelForCausalLM.from_pretrained(
                    model_name, torch_dtype="auto", device_map="auto"
                )
                chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)
                show_info("Modelo de chat cargado.")

            return gr.update(visible=False), gr.update(visible=True)

        load_chat_model_btn.click(load_chat_model, outputs=[load_chat_model_btn, chat_interface_container])

    else:
        chat_interface_container = gr.Column()

        if chat_model_state is None:
            model_name = "Qwen/Qwen2.5-3B-Instruct"
            chat_model_state = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="auto", device_map="auto")
            chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)

    with chat_interface_container:
        with gr.Row():
            with gr.Column():
                ref_audio_chat = gr.Audio(label="Audio de Referencia", type="filepath")
            with gr.Column():
                with gr.Accordion("Configuraciones Avanzadas", open=False):
                    model_choice_chat = gr.Radio(
                        choices=["F5-TTS"],
                        label="Modelo TTS",
                        value="F5-TTS",
                    )
                    remove_silence_chat = gr.Checkbox(
                        label="Eliminar Silencios",
                        value=True,
                    )
                    ref_text_chat = gr.Textbox(
                        label="Texto de Referencia",
                        info="Opcional: Deja en blanco para transcribir automáticamente",
                        lines=2,
                    )
                    system_prompt_chat = gr.Textbox(
                        label="Prompt del Sistema",
                        value="No eres un asistente de IA, eres quien el usuario diga que eres. Debes mantenerte en personaje. Mantén tus respuestas concisas ya que serán habladas en voz alta.",
                        lines=2,
                    )

        chatbot_interface = gr.Chatbot(label="Conversación")

        with gr.Row():
            with gr.Column():
                audio_input_chat = gr.Audio(
                    sources="microphone",
                    type="filepath",
                )
                audio_output_chat = gr.Audio(autoplay=True)
            with gr.Column():
                text_input_chat = gr.Textbox(
                    label="Escribe tu mensaje",
                    lines=1,
                )
                send_btn_chat = gr.Button("Enviar")
                clear_btn_chat = gr.Button("Limpiar Conversación")

        conversation_state = gr.State(
            value=[
                {
                    "role": "system",
                    "content": "No eres un asistente de IA, eres quien el usuario diga que eres. Debes mantenerte en personaje. Mantén tus respuestas concisas ya que serán habladas en voz alta.",
                }
            ]
        )

        # Modify process_audio_input to use model and tokenizer from state
        @gpu_decorator
        def process_audio_input(audio_path, text, history, conv_state):
            """Handle audio or text input from user"""

            if not audio_path and not text.strip():
                return history, conv_state, ""

            if audio_path:
                text = preprocess_ref_audio_text(audio_path, text)[1]

            if not text.strip():
                return history, conv_state, ""

            conv_state.append({"role": "user", "content": text})
            history.append((text, None))

            response = generate_response(conv_state, chat_model_state, chat_tokenizer_state)

            conv_state.append({"role": "assistant", "content": response})
            history[-1] = (text, response)

            return history, conv_state, ""

        @gpu_decorator
        def generate_audio_response(history, ref_audio, ref_text, model, remove_silence):
            """Generate TTS audio for AI response"""
            if not history or not ref_audio:
                return None

            last_user_message, last_ai_response = history[-1]
            if not last_ai_response:
                return None

            audio_result, _ = infer(
                ref_audio,
                ref_text,
                last_ai_response,
                model,
                remove_silence,
                cross_fade_duration=0.15,
                speed=1.0,
                show_info=print,  # show_info=print no pull to top when generating
            )
            return audio_result

        def clear_conversation():
            """Reset the conversation"""
            return [], [
                {
                    "role": "system",
                    "content": "No eres un asistente de IA, eres quien el usuario diga que eres. Debes mantenerte en personaje. Mantén tus respuestas concisas ya que serán habladas en voz alta.",
                }
            ]

        def update_system_prompt(new_prompt):
            """Update the system prompt and reset the conversation"""
            new_conv_state = [{"role": "system", "content": new_prompt}]
            return [], new_conv_state

        # Handle audio input
        audio_input_chat.stop_recording(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=[audio_output_chat],
        ).then(
            lambda: None,
            None,
            audio_input_chat,
        )

        # Handle text input
        text_input_chat.submit(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=[audio_output_chat],
        ).then(
            lambda: None,
            None,
            text_input_chat,
        )

        # Handle send button
        send_btn_chat.click(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, model_choice_chat, remove_silence_chat],
            outputs=[audio_output_chat],
        ).then(
            lambda: None,
            None,
            text_input_chat,
        )

        # Handle clear button
        clear_btn_chat.click(
            clear_conversation,
            outputs=[chatbot_interface, conversation_state],
        )

        # Handle system prompt change and reset conversation
        system_prompt_chat.change(
            update_system_prompt,
            inputs=system_prompt_chat,
            outputs=[chatbot_interface, conversation_state],
        )
########## TERMINA GRADIO PRINCIPAL ##########

with gr.Blocks() as app_credits:
    gr.Markdown("""
# Créditos
* [ΜΕΤΑΝΘΡΩΠΙΑ](https://github.com/METANTROP-IA) por el [demo de Mnemosynth](https://www.wikipedia.org)
* [mrfakename](https://github.com/fakerybakery) por el [demo online original](https://huggingface.co/spaces/mrfakename/E2-F5-TTS)
* [RootingInLoad](https://github.com/RootingInLoad) por la generación inicial de fragmentos y exploración de la aplicación de podcast
* [jpgallegoar](https://github.com/jpgallegoar) por la generación de múltiples tipos de habla, chat de voz y afinación en español
""")

with gr.Blocks() as app:

    gr.TabbedInterface(
        [app_chat, app_credits],
        ["Mnemosynth", "Créditos"],
    )

@click.command()
@click.option("--port", "-p", default=None, type=int, help="Puerto para ejecutar la aplicación")
@click.option("--host", "-H", default=None, help="Host para ejecutar la aplicación")
@click.option(
    "--share",
    "-s",
    default=False,
    is_flag=True,
    help="Compartir la aplicación a través de un enlace compartido de Gradio",
)
@click.option("--api", "-a", default=True, is_flag=True, help="Permitir acceso a la API")
def main(port, host, share, api):
    global app
    print("Iniciando la aplicación...")
    app.queue(api_open=api).launch(server_name=host, server_port=port, share=True, show_api=api)


if __name__ == "__main__":
    if not USING_SPACES:
        main()
    else:
        app.queue().launch()
