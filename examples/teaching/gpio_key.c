#include "stm32h7xx_hal.h"

/* Inspect these variables and lesson_step with the normal ARM GDB debugger. */
volatile uint32_t lesson_ready;
volatile uint32_t lesson_error;

static void Error_Handler(void)
{
    lesson_error = 1U;
    __disable_irq();
    for (;;) {
        __WFI();
    }
}

static void SystemClock_Config(void)
{
    RCC_OscInitTypeDef oscillator = {0};
    RCC_ClkInitTypeDef clocks = {0};

    if (HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY) != HAL_OK) {
        Error_Handler();
    }
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE3);
    while (__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY) == RESET) {
        /* The HAL SysTick remains active during clock configuration. */
    }

    oscillator.OscillatorType = RCC_OSCILLATORTYPE_HSI;
    oscillator.HSIState = RCC_HSI_DIV1;
    oscillator.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
    oscillator.PLL.PLLState = RCC_PLL_NONE;
    if (HAL_RCC_OscConfig(&oscillator) != HAL_OK) {
        Error_Handler();
    }

    clocks.ClockType = RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK |
                       RCC_CLOCKTYPE_D1PCLK1 | RCC_CLOCKTYPE_PCLK1 |
                       RCC_CLOCKTYPE_PCLK2 | RCC_CLOCKTYPE_D3PCLK1;
    clocks.SYSCLKSource = RCC_SYSCLKSOURCE_HSI;
    clocks.SYSCLKDivider = RCC_SYSCLK_DIV1;
    clocks.AHBCLKDivider = RCC_HCLK_DIV1;
    clocks.APB3CLKDivider = RCC_APB3_DIV1;
    clocks.APB1CLKDivider = RCC_APB1_DIV1;
    clocks.APB2CLKDivider = RCC_APB2_DIV1;
    clocks.APB4CLKDivider = RCC_APB4_DIV1;
    if (HAL_RCC_ClockConfig(&clocks, FLASH_LATENCY_0) != HAL_OK) {
        Error_Handler();
    }
}

static void GPIO_Config(void)
{
    GPIO_InitTypeDef pin = {0};

    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();

    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_RESET);
    pin.Pin = GPIO_PIN_0;
    pin.Mode = GPIO_MODE_OUTPUT_PP;
    pin.Pull = GPIO_NOPULL;
    pin.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOB, &pin);

    pin.Pin = GPIO_PIN_15;
    pin.Mode = GPIO_MODE_INPUT;
    pin.Pull = GPIO_PULLUP;
    HAL_GPIO_Init(GPIOA, &pin);
}

/* DM-MC02's user key is active low on PA15; PB0 is a digital output. */
__attribute__((noinline)) void lesson_step(void)
{
    const int pressed = HAL_GPIO_ReadPin(GPIOA, GPIO_PIN_15) == GPIO_PIN_RESET;
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, pressed ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

void SysTick_Handler(void)
{
    HAL_IncTick();
}

int main(void)
{
    if (HAL_Init() != HAL_OK) {
        Error_Handler();
    }
    SystemClock_Config();
    GPIO_Config();
    lesson_ready = 1U;

    for (;;) {
        lesson_step();
        HAL_Delay(1U);
    }
}
