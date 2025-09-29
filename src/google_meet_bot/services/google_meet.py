import os
import time
import logging
from typing import Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .base import VideoConferenceProvider

load_dotenv()

logger = logging.getLogger(__name__)


class GoogleMeetProvider(VideoConferenceProvider):
    """Реализация провайдера для Google Meet на базе Selenium."""

    def __init__(self):
        self.mail_address = os.getenv("EMAIL_ID")
        self.password = os.getenv("EMAIL_PASSWORD")
        opt = Options()
        opt.add_argument("--disable-blink-features=AutomationControlled")
        opt.add_argument("--start-maximized")
        opt.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.media_stream_mic": 1,
                "profile.default_content_setting_values.media_stream_camera": 1,
                "profile.default_content_setting_values.geolocation": 0,
                "profile.default_content_setting_values.notifications": 1,
            },
        )
        self.driver = webdriver.Chrome(options=opt)

    def login(self, email: Optional[str] = None, password: Optional[str] = None) -> None:
        """Авторизация в Google."""
        # Skip authorization if no credentials provided (guest mode)
        if not email or not password:
            logger.info("Google login skipped: using guest flow (no credentials provided)")
            return
            
        # Use provided credentials or fall back to instance variables
        mail = email or self.mail_address
        pwd = password or self.password
        if not mail or not pwd:
            raise RuntimeError("EMAIL_ID и EMAIL_PASSWORD обязательны для входа в Google")

        self.driver.get(
            "https://accounts.google.com/ServiceLogin?hl=en&passive=true&continue=https://www.google.com/&ec=GAZAAQ"
        )
        # Ввод email
        WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.ID, "identifierId"))).send_keys(mail)
        WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable((By.ID, "identifierNext"))).click()
        # Ввод пароля
        WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="password"]/div[1]/div/div[1]/input'))
        ).send_keys(pwd)
        WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable((By.ID, "passwordNext"))).click()
        # Переход на главную Google для нормализации сессии
        self.driver.get("https://google.com/")
        logger.info("Gmail login activity: Done")

    def pre_join_setup(self, meeting_link: str) -> None:
        """Открыть ссылку Meet и выключить микрофон/камеру."""
        self.driver.get(meeting_link)
        time.sleep(5)  # Ждем загрузку страницы
        
        # Сохраняем скриншот для отладки
        self.driver.save_screenshot("google_meet_setup.png")
        
        # Проверяем, есть ли поле для ввода имени гостя
        try:
            # Попробуем найти поле для ввода имени гостя
            guest_name_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="text"]'))
            )
            guest_name_input.send_keys("Bot Guest")
            logger.info("Guest name entered: Bot Guest")
            time.sleep(1)
        except TimeoutException:
            logger.info("No guest name input found, proceeding...")
        
        # Выключение микрофона
        try:
            WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'div[jscontroller="t2mBxb"][data-anchor-id="hw0c9"]'))
            ).click()
            logger.info("Turn off mic activity: Done")
        except TimeoutException:
            logger.warning("Mic button not found, skipping...")
        
        # Выключение камеры
        try:
            time.sleep(1)
            WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'div[jscontroller="bwqwSd"][data-anchor-id="psRWwc"]'))
            ).click()
            logger.info("Turn off camera activity: Done")
        except TimeoutException:
            logger.warning("Camera button not found, skipping...")

    def join(self, meeting_link: str) -> None:
        """Нажать кнопку 'Ask to join' или 'Join now'."""
        # Страница уже открыта в pre_join_setup
        time.sleep(3)
        
        # Сохраняем скриншот для отладки
        self.driver.save_screenshot("google_meet_join.png")
        
        # Пробуем разные селекторы для кнопки входа
        join_selectors = [
            'button[jsname="Qx7uuf"]',  # Ask to join
            'button[jsname="BOHaEe"]',  # Join now
            'button[data-mdc-dialog-action="join"]',
            'button[aria-label*="Join"]',
            'button[aria-label*="join"]',
            '//button[contains(text(), "Join")]',
            '//button[contains(text(), "Ask")]',
        ]
        
        for selector in join_selectors:
            try:
                if selector.startswith('//'):
                    # XPath селектор
                    element = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    # CSS селектор
                    element = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                
                element.click()
                logger.info(f"Join button clicked using selector: {selector}")
                time.sleep(2)
                return
                
            except (TimeoutException, NoSuchElementException):
                logger.debug(f"Join button not found with selector: {selector}")
                continue
        
        # Если не нашли кнопку, пробуем нажать Enter
        logger.warning("No join button found, trying to press Enter key...")
        from selenium.webdriver.common.keys import Keys
        self.driver.switch_to.active_element.send_keys(Keys.ENTER)
        logger.info("Enter key pressed as fallback")

    def wait_until_joined(self, timeout_sec: int = 60) -> bool:
        """Подождать индикатор присоединения (эвристика на основе селектора)."""
        try:
            WebDriverWait(self.driver, timeout_sec).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div.uArJ5e.UQuaGc.Y5sE8d.uyXBBb.xKiqt'))
            )
            logger.info("Meeting has been joined")
            return True
        except (TimeoutException, NoSuchElementException):
            logger.info("Meeting has not been joined")
            return False

    def leave(self) -> None:
        """Покинуть встречу (опционально). На данном этапе не реализовано специфичное действие."""
        # Можно добавить клик по кнопке выхода, когда появится стабильный селектор.
        logger.info("Leave meeting (noop)")

    def close(self) -> None:
        """Закрыть браузер и освободить ресурсы."""
        try:
            self.driver.quit()
        except Exception:
            pass