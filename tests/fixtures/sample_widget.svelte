<script>
  let count = 0;
  function increment() {
    count += 1;
  }
</script>

<button class="btn btn--primary" on:click={increment}>+</button>
<div class:active={count > 0} class="counter">{count}</div>

<style lang="scss">
  .btn {
    color: red;
  }
  .btn--primary {
    font-weight: bold;
  }
  .counter {
    padding: 4px;
  }
</style>
