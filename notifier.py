def send_notification(title: str, message: str) -> None:
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name='Deal Scraper',
            timeout=8,
        )
    except Exception:
        pass
