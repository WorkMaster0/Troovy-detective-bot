from quantum_shadow import setup_shadow_handlers

def main():
    application = Application.builder().token("YOUR_TOKEN").build()
    setup_shadow_handlers(application)
    application.run_polling()